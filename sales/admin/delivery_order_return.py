"""Returns of goods from a delivery order, and the reasons they come back for.

A return is opened from the delivery order ("Register Return"), its lines are
filled while it is a draft, and the buttons move it on; receiving it is what
puts the stock back (see DeliveryOrderReturnService).
"""
from collections import defaultdict

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action, display
from unfold.enums import ActionVariant
from unfold.widgets import UnfoldAdminSelect2MultipleWidget

from core.admin.company_access import get_user_companies
from core.admin.company_admin import CompanyRestrictedAdmin
from inventory.models import ItemIdentityCode
from sales.models import (
    DELIVERY_ORDER_STOCK_OUT_STATUSES,
    DeliveryOrder,
    DeliveryOrderReturn,
    DeliveryOrderReturnCode,
    DeliveryOrderReturnItem,
    DeliveryOrderReturnStatus,
    ReturnReason,
)
from sales.services.delivery_order_return_service import DeliveryOrderReturnService

S = DeliveryOrderReturnStatus


@admin.register(ReturnReason)
class ReturnReasonAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["name", "restocks", "is_active"]
    list_filter = ["restocks", "is_active"]
    search_fields = ["name"]
    autocomplete_fields = ["company"]
    fields = ["company", "name", "restocks", "is_active"]


def _delivery_order_for(request):
    """The delivery order the return on screen belongs to: the saved
    return's, or the one the add page was opened for."""
    object_id = request.resolver_match.kwargs.get("object_id") if request.resolver_match else None
    if object_id:
        return (
            DeliveryOrder.objects.filter(returns__pk=object_id).select_related("company").first()
        )
    delivery_order_id = request.POST.get("delivery_order") or request.GET.get("delivery_order")
    if delivery_order_id and str(delivery_order_id).isdigit():
        return DeliveryOrder.objects.filter(
            pk=delivery_order_id, company__in=get_user_companies(request.user),
        ).select_related("company").first()
    return None


class ReturnLineChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, line):
        return f"{line.item_number} · {line.description} — {_('shipped')} {DeliveryOrderReturnService.shipped_units(line)}"


class IdentityCodeChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, code):
        line = code.delivery_order_item
        return f"{code.code} · {code.item.code}" + (f" ({line.item_number})" if line else "")


class DeliveryOrderReturnItemForm(forms.ModelForm):
    """A return line plus the identity codes that came back with it.

    The codes are kept as DeliveryOrderReturnCode rows recording the line
    each one was shipped on; the codes stay linked to the delivery order
    until the return is received.
    """

    codes = IdentityCodeChoiceField(
        label=_("Returned item codes"),
        queryset=ItemIdentityCode.objects.none(),
        required=False,
        widget=UnfoldAdminSelect2MultipleWidget,
        help_text=_("Codes linked to this line or its components; they are unlinked when the return is received."),
    )

    #: Set by the inline from the delivery order on screen.
    delivery_order = None

    class Meta:
        model = DeliveryOrderReturnItem
        fields = ["delivery_order_item", "quantity", "reason"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        picked = (
            list(self.instance.returned_codes.values_list("identity_code_id", flat=True))
            if self.instance.pk else []
        )
        linked = ItemIdentityCode.objects.filter(
            delivery_order_item__delivery_order=self.delivery_order,
        ) if self.delivery_order else ItemIdentityCode.objects.none()
        self.fields["codes"].queryset = (
            linked | ItemIdentityCode.objects.filter(pk__in=picked)
        ).select_related("item", "delivery_order_item").order_by("code").distinct()
        self.initial.setdefault("codes", picked)

    def save(self, commit=True):
        line = super().save(commit=commit)
        if commit:
            self._save_codes(line)
        else:
            parent_save_m2m = self.save_m2m

            def save_m2m():
                parent_save_m2m()
                self._save_codes(line)

            self.save_m2m = save_m2m
        return line

    def _save_codes(self, line):
        codes = {code.pk: code for code in self.cleaned_data.get("codes") or []}
        line.returned_codes.exclude(identity_code_id__in=codes).delete()
        kept = set(line.returned_codes.values_list("identity_code_id", flat=True))
        DeliveryOrderReturnCode.objects.bulk_create([
            DeliveryOrderReturnCode(
                return_item=line, identity_code=code, delivery_order_item_id=code.delivery_order_item_id,
            )
            for pk, code in codes.items() if pk not in kept
        ])


class DeliveryOrderReturnItemFormSet(BaseInlineFormSet):
    """Refuses more units than are still out, and codes that do not belong
    to the line or outnumber what comes back, so the problem shows on the
    draft rather than only when the return is received."""

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        delivery_order = getattr(self.instance, "delivery_order", None)
        if delivery_order is None:
            return
        requested, selections = defaultdict(int), []
        for form in self.forms:
            data = getattr(form, "cleaned_data", None)
            if not data or data.get("DELETE") or not data.get("delivery_order_item"):
                continue
            requested[data["delivery_order_item"]] += data.get("quantity") or 0
            selections.append((data["delivery_order_item"], data.get("quantity"), data.get("codes") or []))
        errors = DeliveryOrderReturnService.check_quantities(
            delivery_order, requested, exclude_return=self.instance,
        )
        errors += DeliveryOrderReturnService.check_codes(selections)
        if errors:
            raise ValidationError(errors)


class DeliveryOrderReturnItemInline(TabularInline):
    model = DeliveryOrderReturnItem
    form = DeliveryOrderReturnItemForm
    formset = DeliveryOrderReturnItemFormSet
    extra = 0
    readonly_fields = ["stock_returned", "codes_display"]
    verbose_name = _("Returned line")
    verbose_name_plural = _("Returned lines")

    def get_fields(self, request, obj=None):
        # Once received the line is read-only, and a form-only field has
        # nothing to display: the stored codes are shown instead.
        codes = "codes" if self._editable(obj) else "codes_display"
        return ["delivery_order_item", "quantity", "reason", codes, "stock_returned"]

    def get_formset(self, request, obj=None, **kwargs):
        form = type(self.form.__name__, (self.form,), {"delivery_order": _delivery_order_for(request)})
        return super().get_formset(request, obj, form=form, **kwargs)

    @display(description=_("Returned item codes"))
    def codes_display(self, obj):
        return ", ".join(link.identity_code.code for link in obj.returned_codes.select_related("identity_code")) or "—"

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        delivery_order = _delivery_order_for(request)
        if db_field.name == "delivery_order_item":
            kwargs["queryset"] = (
                DeliveryOrderReturnService.returnable_lines(delivery_order).order_by("pk")
                if delivery_order else db_field.related_model.objects.none()
            )
            kwargs["form_class"] = ReturnLineChoiceField
        elif db_field.name == "reason":
            kwargs["queryset"] = ReturnReason.objects.filter(
                company_id=delivery_order.company_id if delivery_order else None, is_active=True,
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def _editable(self, obj):
        return obj is None or obj.is_editable

    def has_add_permission(self, request, obj=None):
        return self._editable(obj) and super().has_add_permission(request, obj)

    def has_change_permission(self, request, obj=None):
        return self._editable(obj) and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._editable(obj) and super().has_delete_permission(request, obj)


class DeliveryOrderReturnForm(forms.ModelForm):
    class Meta:
        model = DeliveryOrderReturn
        fields = ["delivery_order", "date", "notes"]

    def clean_delivery_order(self):
        delivery_order = self.cleaned_data["delivery_order"]
        if delivery_order.status not in DELIVERY_ORDER_STOCK_OUT_STATUSES:
            raise ValidationError(_("Only goods of a dispatched or delivered delivery order can be returned."))
        return delivery_order


@admin.register(DeliveryOrderReturn)
class DeliveryOrderReturnAdmin(CompanyRestrictedAdmin, ModelAdmin):
    form = DeliveryOrderReturnForm
    list_display = ["number", "get_status_label", "delivery_order", "get_client", "date", "received_at"]
    list_filter = ["status"]
    list_select_related = ["delivery_order__client"]
    search_fields = ["number", "delivery_order__number", "delivery_order__client__name"]
    autocomplete_fields = ["delivery_order"]
    inlines = [DeliveryOrderReturnItemInline]
    actions_detail = [
        {
            "title": _("Status"),
            "icon": "assignment_return",
            "variant": ActionVariant.PRIMARY,
            "items": ["receive_detail_action", "cancel_detail_action", "reopen_detail_action"],
        },
    ]

    def get_fields(self, request, obj=None):
        return ["number", "delivery_order", "date", "status", "received_at", "notes"]

    def get_readonly_fields(self, request, obj=None):
        readonly = ["number", "status", "received_at"]
        if obj is not None:
            readonly.append("delivery_order")
            if not obj.is_editable:
                readonly += ["date", "notes"]
        return readonly

    def get_inlines(self, request, obj):
        # Lines are picked from the delivery order's own lines: without one
        # there is nothing to offer yet.
        if obj is None and _delivery_order_for(request) is None:
            return []
        return super().get_inlines(request, obj)

    def response_add(self, request, obj, post_url_continue=None):
        # Land on the saved return, where its lines and buttons are.
        self.message_user(request, _("Return %(number)s created as a draft.") % {"number": obj.number})
        return HttpResponseRedirect(reverse("admin:sales_deliveryorderreturn_change", args=[obj.pk]))

    @display(
        description=_("Status"),
        ordering="status",
        label={S.DRAFT: "warning", S.RECEIVED: "success", S.CANCELLED: "danger"},
    )
    def get_status_label(self, obj):
        return obj.status, obj.get_status_display()

    @display(description=_("Client"), ordering="delivery_order__client__name")
    def get_client(self, obj):
        return obj.delivery_order.client

    # ── Lifecycle buttons ───────────────────────────────────────────────

    def _status_in(self, request, object_id, statuses):
        if not object_id or not self.has_change_permission(request):
            return False
        return self.get_queryset(request).filter(pk=object_id, status__in=statuses).exists()

    def _run_step(self, request, object_id, step, message):
        ret = self.get_queryset(request).get(pk=object_id)
        try:
            step(ret)
        except ValidationError as e:
            self.message_user(request, " ".join(e.messages), level="error")
        else:
            self.message_user(request, message % {"number": ret.number})
        return redirect(reverse("admin:sales_deliveryorderreturn_change", args=[object_id]))

    @action(description=_("Mark as Received"), icon="move_to_inbox", permissions=["receive"])
    def receive_detail_action(self, request, object_id):
        return self._run_step(
            request, object_id,
            lambda ret: DeliveryOrderReturnService.receive(ret, user=request.user),
            _("Return %(number)s received; its goods are back in stock."),
        )

    def has_receive_permission(self, request, object_id=None):
        return self._status_in(request, object_id, [S.DRAFT])

    @action(description=_("Cancel Return"), icon="block", permissions=["cancel_return"])
    def cancel_detail_action(self, request, object_id):
        return self._run_step(
            request, object_id,
            lambda ret: DeliveryOrderReturnService.cancel(ret, user=request.user),
            _("Return %(number)s cancelled; any stock it brought back has left again."),
        )

    def has_cancel_return_permission(self, request, object_id=None):
        return self._status_in(request, object_id, [S.DRAFT, S.RECEIVED])

    @action(description=_("Reopen"), icon="restart_alt", permissions=["reopen_return"])
    def reopen_detail_action(self, request, object_id):
        return self._run_step(
            request, object_id, DeliveryOrderReturnService.reopen,
            _("Return %(number)s reopened as a draft."),
        )

    def has_reopen_return_permission(self, request, object_id=None):
        return self._status_in(request, object_id, [S.CANCELLED])


class DeliveryOrderReturnInline(TabularInline):
    """Read-only list of the returns raised against a delivery order."""

    model = DeliveryOrderReturn
    extra = 0
    max_num = 0
    can_delete = False
    tab = True
    show_change_link = True
    verbose_name = pgettext_lazy("goods return", "Return")
    verbose_name_plural = _("Returns")
    fields = ["number", "date", "status", "received_at", "lines_display"]
    readonly_fields = fields

    @display(description=pgettext_lazy("delivery order return", "Returned"))
    def lines_display(self, obj):
        parts = []
        for line in obj.items.select_related("delivery_order_item", "reason").prefetch_related(
            "returned_codes__identity_code",
        ):
            text = f"{line.delivery_order_item.item_number} ×{line.quantity} ({line.reason})"
            codes = [link.identity_code.code for link in line.returned_codes.all()]
            if codes:
                text += f" [{', '.join(codes)}]"
            parts.append(text)
        return ", ".join(parts) or "—"

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
