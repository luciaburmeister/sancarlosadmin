from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse, path
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, StackedInline, TabularInline, GenericTabularInline
from unfold.contrib.inlines.admin import NonrelatedTabularInline
from unfold.decorators import action, display
from unfold.enums import ActionVariant
from unfold.contrib.filters.admin import (
    AutocompleteSelectMultipleFilter,
    MultipleChoicesDropdownFilter,
    RangeDateFilter,
)

from core.admin.company_admin import CompanyRestrictedAdmin
from invoices.models import AppliedTax
from core.admin.company_access import get_user_companies
from core.admin.email_log import EmailLogInline
from company.forms import BaseDocumentForm
from inventory.models import ItemIdentityCode
from sales.models import (
    DeliveryOrder, DeliveryOrderItem, DeliveryOrderStatus,
    DeliveryOrderDocument, DeliveryOrderDocumentType, ClientAddress,
)
from sales.views.delivery_order_export_view import DeliveryOrderExportView
from sales.views.delivery_order_send_email_view import DeliveryOrderSendEmailView
from sales.views.delivery_order_fulfillment_views import (
    DeliveryOrderConfirmDeliveryView,
    DeliveryOrderDispatchView,
)
from sales.services.delivery_order_fulfillment_service import (
    PREVIOUS_STATUS,
    DeliveryOrderFulfillmentService,
)
from sales.views.delivery_order_identity_codes_view import (
    DeliveryOrderLinkIdentityCodesView,
    DeliveryOrderUnlinkIdentityCodeView,
)


class DeliveryOrderDocumentForm(BaseDocumentForm):
    class Meta:
        model = DeliveryOrderDocument
        fields = "__all__"


class DeliveryOrderDocumentInline(StackedInline):
    model = DeliveryOrderDocument
    form = DeliveryOrderDocumentForm
    extra = 0
    tab = True
    exclude = ["company"]
    autocomplete_fields = ["document_type"]

    fieldsets = (
        (None, {
            "fields": (
                ("document_type", "file"),
                ("does_not_expire", "is_active"),
                ("issue_date", "expiry_date"),
                "notes",
            )
        }),
    )


@admin.register(DeliveryOrderDocumentType)
class DeliveryOrderDocumentTypeAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("name", "company")
    search_fields = ("name",)
    autocomplete_fields = ["company"]


class IdentityCodeDeliveryInline(TabularInline):
    model = ItemIdentityCode
    extra = 0
    verbose_name = _("Identity Code")
    verbose_name_plural = _("Identity Codes")
    fields = ("code", "item", "delivered_at")
    readonly_fields = ("code", "item", "delivered_at")
    tab=True

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class DeliveryOrderIdentityCodeInline(NonrelatedTabularInline):
    """Read-only traceability — identity codes currently linked to this
    delivery order's line items. Use the "Link Item Codes" action to add
    or remove links."""
    model = ItemIdentityCode
    extra = 0
    max_num = 0
    can_delete = False
    tab = True
    verbose_name = _("Linked Item Code")
    verbose_name_plural = _("Linked Item Codes")
    fields = ["code", "item", "line_item_display", "delivered_at"]
    readonly_fields = ["code", "item", "line_item_display", "delivered_at"]

    def get_form_queryset(self, obj):
        return ItemIdentityCode.objects.filter(
            delivery_order_item__delivery_order=obj
        ).select_related("item", "delivery_order_item").order_by(
            "delivery_order_item_id", "code"
        )

    def save_new_instance(self, parent, instance):
        pass

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @display(description=_("Line Item"))
    def line_item_display(self, obj):
        return obj.delivery_order_item


class DeliveryOrderAppliedTaxInline(GenericTabularInline):
    hide_ordering_field = True
    ordering_field = "order"
    model = AppliedTax
    extra = 0
    autocomplete_fields = ("tax_type",)
    ordering = ("order",)
    fieldset = {
        "Applied tax": (("tax_type", "selected_percentage"), ("base_amount", "calculated_amount"), "order")
    }
    verbose_name = _("Applied tax")
    verbose_name_plural = _("Applied taxes")
    tab = True


class DeliveryOrderItemInline(StackedInline):
    """The order's own lines. Component lines (materialised bill of
    materials) are derived from these and listed read-only in
    DeliveryOrderComponentInline."""

    model = DeliveryOrderItem
    extra = 0
    tab = True
    name = _("Items")
    autocomplete_fields = ["inventory_item"]
    fieldsets = (
        (None, {
            "fields": (
                ("inventory_item", "components_display"),
                ("item_number", "description", "reference"),
                ("quantity", "unit_price"),
                ("discount_percentage", "line_total"),
                ("source_object"),
            )
        }),
    )
    readonly_fields = ["item_number", "line_total", "source_object", "components_display"]

    def get_queryset(self, request):
        return super().get_queryset(request).top_level()

    @display(description=_("Components"))
    def components_display(self, obj):
        if not obj or not obj.pk:
            return "—"
        count = obj.component_lines.count()
        if not count:
            return "—"
        return _("%(count)d component line(s), see the Components tab") % {"count": count}


class DeliveryOrderComponentInline(TabularInline):
    """Read-only view of the component lines exploded from each parent
    line's bill of materials. They are re-derived whenever a parent line's
    quantity changes; codes are linked to them from "Link Item Codes"."""

    model = DeliveryOrderItem
    extra = 0
    max_num = 0
    can_delete = False
    tab = True
    verbose_name = _("Component")
    verbose_name_plural = _("Components")
    fields = ("component_display", "parent_display", "reference", "quantity", "codes_display")
    readonly_fields = fields

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .components()
            .select_related("inventory_item", "parent_line")
            .order_by("pk")
        )

    def get_formset(self, request, obj=None, **kwargs):
        # Same model and FK as DeliveryOrderItemInline: keep the formset
        # prefixes apart so the two do not share one management form.
        formset = super().get_formset(request, obj, **kwargs)
        base_prefix = formset.get_default_prefix()

        class PrefixedFormSet(formset):
            @classmethod
            def get_default_prefix(cls):
                return f"{base_prefix}-components"

        return PrefixedFormSet

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @display(description=_("Component"))
    def component_display(self, obj):
        indent = " " * max(obj.depth - 1, 0)
        return f"{indent}↳ {obj.description}"

    @display(description=_("Component of"))
    def parent_display(self, obj):
        parent = obj.parent_line
        return f"{parent.item_number} {parent.description}" if parent else "—"

    @display(description=_("Codes"))
    def codes_display(self, obj):
        linked = obj.identity_codes.count()
        return f"{linked}/{obj.quantity.normalize():f}"


@admin.register(DeliveryOrder)
class DeliveryOrderAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = [
        "number", "get_status_label", "client", "date", "planned_delivery_date",
        "shipping_method", "total_amount", "get_invoiced_label",
    ]
    list_filter = [
        ("status", MultipleChoicesDropdownFilter),
        ("shipping_method", MultipleChoicesDropdownFilter),
        ("date", RangeDateFilter),
        ("planned_delivery_date", RangeDateFilter),
    ]
    # get_invoiced_label reads the invoice's status
    list_select_related = ["client", "sales_invoice"]
    search_fields = ["number", "external_id", "client__name", "shipment_number", "shipping_agency__name"]
    autocomplete_fields = ["company", "client", "sales_invoice", "configuration", "project_file", "shipping_agency"]
    inlines = [
        DeliveryOrderItemInline,
        DeliveryOrderComponentInline,
        DeliveryOrderAppliedTaxInline,
        DeliveryOrderIdentityCodeInline,
        DeliveryOrderDocumentInline,
        EmailLogInline,
    ]
    # The status moves only through the shipping buttons, which also move
    # the stock (see DeliveryOrderFulfillmentService).
    readonly_fields = [
        "status", "subtotal_amount", "lines_total_display",
        "taxable_base_display", "discount_amount", "tax_amount", "total_amount",
    ]
    list_filter_submit = True

    @display(description=_("Lines total (before discount)"))
    def lines_total_display(self, obj):
        return obj.subtotal_amount

    @display(description=_("Subtotal"))
    def taxable_base_display(self, obj):
        return obj.taxable_base

    fieldsets = (
        (_("General"), {
            "classes": ["tab"],
            "fields": (
                ("number", "date"),
                ("company", "client"),
                ("project_file", "external_id"),
                ("status", "lines_total_display"),
                ("discount_amount", "taxable_base_display"),
                ("tax_amount", "total_amount"),
                "sales_invoice",
                "notes",
            )
        }),
        (_("Shipping"), {
            "classes": ["tab"],
            "fields": (
                ("shipping_method", "planned_delivery_date"),
                "delivery_address",
                ("package_count", "shipping_agency"),
                ("shipment_number", "tracking_url"),
                ("dispatched_at", "delivered_at"),
                "received_by",
            )
        }),
    )

    actions_row = ["create_invoice_from_do_row_action", "export_delivery_order_action", "send_email_row_action"]
    # Grouped into dropdowns so the change page header stays readable; a
    # dropdown whose actions are all hidden for this order is not rendered.
    actions_detail = [
        {
            "title": _("Shipping"),
            "icon": "local_shipping",
            "variant": ActionVariant.PRIMARY,
            "items": [
                "mark_prepared_detail_action",
                "dispatch_detail_action",
                "confirm_delivery_detail_action",
                "undo_status_detail_action",
                "cancel_detail_action",
                "reopen_detail_action",
            ],
        },
        {
            "title": _("PDF & Email"),
            "icon": "description",
            "items": [
                "export_delivery_order_detail_action",
                "send_email_detail_action",
            ],
        },
        "create_invoice_from_do_detail_action",
        {
            "title": _("More"),
            "icon": "more_horiz",
            "items": [
                "link_identity_codes_detail_action",
                "sync_from_quote_detail_action",
            ],
        },
    ]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """
        The delivery_address dropdown only lists addresses belonging to
        this delivery order's own client, sorted with the default one first.
        """
        if db_field.name == "delivery_address":
            object_id = request.resolver_match.kwargs.get("object_id") if request.resolver_match else None
            client_id = None
            if object_id:
                do = DeliveryOrder.objects.filter(pk=object_id).only("client_id").first()
                client_id = do.client_id if do else None
            else:
                client_id = request.GET.get("client")

            if client_id:
                kwargs["queryset"] = ClientAddress.objects.filter(
                    client_id=client_id
                ).order_by("-is_default", "address_type")
            else:
                qs = ClientAddress.objects.all()
                if not request.user.is_superuser:
                    qs = qs.filter(company__in=get_user_companies(request.user))
                kwargs["queryset"] = qs.order_by("client__name", "-is_default")

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<int:delivery_order_id>/export/",
                self.admin_site.admin_view(DeliveryOrderExportView.as_view(model_admin=self)),
                name="sales_deliveryorder_export",
            ),
            path(
                "<int:delivery_order_id>/send-email/",
                self.admin_site.admin_view(DeliveryOrderSendEmailView.as_view(model_admin=self)),
                name="sales_deliveryorder_send_email",
            ),
            path(
                "<int:delivery_order_id>/dispatch/",
                self.admin_site.admin_view(DeliveryOrderDispatchView.as_view(model_admin=self)),
                name="sales_deliveryorder_dispatch",
            ),
            path(
                "<int:delivery_order_id>/confirm-delivery/",
                self.admin_site.admin_view(DeliveryOrderConfirmDeliveryView.as_view(model_admin=self)),
                name="sales_deliveryorder_confirm_delivery",
            ),
            path(
                "<int:delivery_order_id>/link-identity-codes/",
                self.admin_site.admin_view(DeliveryOrderLinkIdentityCodesView.as_view(model_admin=self)),
                name="sales_deliveryorder_link_identity_codes",
            ),
            path(
                "<int:delivery_order_id>/unlink-identity-code/<int:code_id>/",
                self.admin_site.admin_view(DeliveryOrderUnlinkIdentityCodeView.as_view(model_admin=self)),
                name="sales_deliveryorder_unlink_identity_code",
            ),
        ]
        return custom + urls

    @display(
        description=_("Status"),
        ordering="status",
        label={
            DeliveryOrderStatus.DRAFT: "warning",
            DeliveryOrderStatus.PREPARED: "primary",
            DeliveryOrderStatus.DISPATCHED: "info",
            DeliveryOrderStatus.DELIVERED: "success",
            DeliveryOrderStatus.CANCELLED: "danger",
        },
    )
    def get_status_label(self, obj):
        return obj.status, obj.get_status_display()

    @display(
        description=_("Invoiced"),
        label={True: "success", False: "warning"},  # see get_status_label
    )
    def get_invoiced_label(self, obj):
        from invoices.services.delivery_order_service import DeliveryOrderService

        # A cancelled invoice bills nothing, as in the invoiceable filter
        invoiced = DeliveryOrderService.has_active_invoice(obj)
        return invoiced, _("Yes") if invoiced else _("No")

    def save_related(self, request, form, formsets, change):
        from invoices.services.delivery_order_service import DeliveryOrderService

        super().save_related(request, form, formsets, change)
        delivery_order = form.instance

        # Line edits re-derive the carried discount and tax bases. Legacy DOs
        # are priced net of the discount and must be left alone.
        if self._items_changed(formsets) and DeliveryOrderService._is_self_contained(delivery_order):
            DeliveryOrderService.recompute_discounts_and_taxes(delivery_order)
        else:
            delivery_order.recalculate_totals()

    @staticmethod
    def _items_changed(formsets):
        """Whether this submission added, edited or removed a line item.

        A submission that only touches the applied-tax inline keeps the taxes
        the user just entered instead of re-deriving over them.
        """
        for formset in formsets:
            if formset.model is not DeliveryOrderItem:
                continue
            if (
                getattr(formset, "new_objects", None)
                or getattr(formset, "changed_objects", None)
                or getattr(formset, "deleted_objects", None)
            ):
                return True
        return False

    @action(description=_("Create Invoice"), icon="receipt_long")
    def create_invoice_from_do_row_action(self, request, object_id):
        from django.core.exceptions import ValidationError
        from invoices.services.advances import AdvanceDeductionService
        from invoices.services.delivery_order_service import DeliveryOrderService
        from django.utils import timezone
        from django.contrib import messages as django_messages

        do = DeliveryOrder.objects.select_related(
            "client", "company", "project_file", "sales_invoice",
        ).get(pk=object_id)

        # A cancelled invoice bills nothing: its delivery order can be invoiced again.
        if DeliveryOrderService.has_active_invoice(do):
            django_messages.warning(request, _("This delivery order is already linked to an invoice."))
            return redirect(reverse("admin:sales_deliveryorder_changelist"))

        try:
            invoice = DeliveryOrderService.create_invoice_from_delivery_orders(
                delivery_orders=[do],
                company=do.company,
                client=do.client,
                date_issued=timezone.localdate(),
            )
            deductions = AdvanceDeductionService.deduction_lines(invoice)
            if deductions:
                django_messages.success(
                    request,
                    _("Invoice created from Delivery Order %(number)s, deducting %(amount)s "
                      "of advances (%(advances)s).") % {
                        "number": do.number, **AdvanceDeductionService.describe(deductions),
                    },
                )
            else:
                django_messages.success(
                    request,
                    _("Invoice created from Delivery Order %(number)s.") % {"number": do.number},
                )
            return redirect(reverse("admin:invoices_salesinvoice_change", args=[invoice.id]))
        except ValidationError as e:
            django_messages.error(request, " ".join(e.messages))
            return redirect(reverse("admin:sales_deliveryorder_changelist"))
        except Exception as e:
            django_messages.error(request, str(e))
            return redirect(reverse("admin:sales_deliveryorder_changelist"))

    @action(description=_("Create Invoice"), icon="receipt_long")
    def create_invoice_from_do_detail_action(self, request, object_id):
        return self.create_invoice_from_do_row_action(request, object_id)

    @action(description=_("Export"), icon="picture_as_pdf", attrs={"target": "_blank"})
    def export_delivery_order_action(self, request, object_id: int):
        return redirect(reverse("admin:sales_deliveryorder_export", args=[object_id]))

    @action(description=_("Export"), icon="picture_as_pdf")
    def export_delivery_order_detail_action(self, request, object_id: int):
        return redirect(reverse("admin:sales_deliveryorder_export", args=[object_id]))

    @action(description=_("Send Email"), icon="mail")
    def send_email_row_action(self, request, object_id):
        url = reverse("admin:sales_deliveryorder_send_email", args=[object_id])
        return redirect(f"{url}?redirect_to=changelist")

    @action(description=_("Send Email"), icon="mail")
    def send_email_detail_action(self, request, object_id):
        return redirect(reverse("admin:sales_deliveryorder_send_email", args=[object_id]))

    @action(description=_("Link Item Codes"), icon="qr_code_2")
    def link_identity_codes_detail_action(self, request, object_id):
        return redirect(reverse("admin:sales_deliveryorder_link_identity_codes", args=[object_id]))

    @action(
        description=_("Sync from Quote"),
        icon="sync",
        permissions=["sync_from_quote"],
        url_path="sync-from-quote",
    )
    def sync_from_quote_detail_action(self, request, object_id):
        """Re-derives the lines, discount and taxes from the current state of
        the source quotes, so a stale delivery order does not have to be
        deleted and created again. Superusers only."""
        from django.core.exceptions import ValidationError
        from invoices.services.delivery_order_service import DeliveryOrderService

        do = DeliveryOrder.objects.select_related("project_file").get(pk=object_id)
        try:
            counts = DeliveryOrderService.sync_from_sources(do)
        except ValidationError as e:
            self.message_user(request, " ".join(e.messages), level="error")
        else:
            self.message_user(
                request,
                _(
                    "Delivery Order %(number)s synced from its quotes: "
                    "%(updated)d line(s) updated, %(added)d added, %(removed)d removed."
                ) % {"number": do.number, **counts},
            )
        return redirect(reverse("admin:sales_deliveryorder_change", args=[object_id]))

    def has_sync_from_quote_permission(self, request, object_id=None):
        return request.user.is_superuser

    # ── Shipping lifecycle ──────────────────────────────────────────────

    def _status_in(self, request, object_id, statuses):
        """Shows a shipping button only on orders it applies to; the service
        re-checks the transition when the button is pressed."""
        if not object_id or not self.has_change_permission(request):
            return False
        return DeliveryOrder.objects.filter(pk=object_id, status__in=statuses).exists()

    def _run_step(self, request, object_id, step, message):
        from django.core.exceptions import ValidationError

        do = DeliveryOrder.objects.get(pk=object_id)
        try:
            step(do)
        except ValidationError as e:
            self.message_user(request, " ".join(e.messages), level="error")
        else:
            self.message_user(request, message % {"number": do.number, "status": do.get_status_display()})
        return redirect(reverse("admin:sales_deliveryorder_change", args=[object_id]))

    @action(description=_("Mark as Prepared"), icon="inventory_2", permissions=["mark_prepared"])
    def mark_prepared_detail_action(self, request, object_id):
        return self._run_step(
            request, object_id, DeliveryOrderFulfillmentService.mark_prepared,
            _("Delivery Order %(number)s is prepared and ready to dispatch."),
        )

    def has_mark_prepared_permission(self, request, object_id=None):
        return self._status_in(request, object_id, [DeliveryOrderStatus.DRAFT])

    @action(description=_("Dispatch"), icon="local_shipping", permissions=["dispatch"])
    def dispatch_detail_action(self, request, object_id):
        return redirect(reverse("admin:sales_deliveryorder_dispatch", args=[object_id]))

    def has_dispatch_permission(self, request, object_id=None):
        return self._status_in(request, object_id, [DeliveryOrderStatus.DRAFT, DeliveryOrderStatus.PREPARED])

    @action(description=_("Confirm Delivery"), icon="task_alt", permissions=["confirm_delivery"])
    def confirm_delivery_detail_action(self, request, object_id):
        return redirect(reverse("admin:sales_deliveryorder_confirm_delivery", args=[object_id]))

    def has_confirm_delivery_permission(self, request, object_id=None):
        return self._status_in(request, object_id, [DeliveryOrderStatus.DISPATCHED])

    @action(description=_("Undo Last Step"), icon="undo", permissions=["undo_status"])
    def undo_status_detail_action(self, request, object_id):
        return self._run_step(
            request, object_id, DeliveryOrderFulfillmentService.undo,
            _("Delivery Order %(number)s is back to “%(status)s”."),
        )

    def has_undo_status_permission(self, request, object_id=None):
        return self._status_in(request, object_id, list(PREVIOUS_STATUS))

    @action(description=_("Cancel Delivery Order"), icon="block", permissions=["cancel_order"])
    def cancel_detail_action(self, request, object_id):
        return self._run_step(
            request, object_id, DeliveryOrderFulfillmentService.cancel,
            _("Delivery Order %(number)s cancelled; any stock it took out is back in the warehouse."),
        )

    def has_cancel_order_permission(self, request, object_id=None):
        return self._status_in(request, object_id, [
            DeliveryOrderStatus.DRAFT, DeliveryOrderStatus.PREPARED, DeliveryOrderStatus.DISPATCHED,
        ])

    @action(description=_("Reopen"), icon="restart_alt", permissions=["reopen"])
    def reopen_detail_action(self, request, object_id):
        return self._run_step(
            request, object_id, DeliveryOrderFulfillmentService.reopen,
            _("Delivery Order %(number)s reopened as pending shipment."),
        )

    def has_reopen_permission(self, request, object_id=None):
        return self._status_in(request, object_id, [DeliveryOrderStatus.CANCELLED])


@admin.register(DeliveryOrderItem)
class DeliveryOrderItemAdmin(ModelAdmin):
    list_display = ["item_number", "description", "quantity", "inventory_item", "parent_line", "delivery_order"]
    search_fields = ["item_number", "description", "delivery_order__number", "inventory_item__code"]
    autocomplete_fields = ["delivery_order", "inventory_item", "parent_line"]
    readonly_fields = ["item_number", "line_total"]
    inlines = [IdentityCodeDeliveryInline]
