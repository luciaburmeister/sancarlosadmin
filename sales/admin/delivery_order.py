from django.contrib import admin
from django.shortcuts import redirect
from django.urls import reverse, path
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, StackedInline, TabularInline, GenericTabularInline
from unfold.contrib.inlines.admin import NonrelatedTabularInline
from unfold.decorators import action, display
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
    model = DeliveryOrderItem
    extra = 0
    tab = True
    name = _("Items")
    fieldsets = (
        (None, {
            "fields": (
                ("item_number", "description", "reference"),
                ("quantity", "unit_price"),
                ("discount_percentage", "line_total"),
                ("source_object"),
            )
        }),
    )
    readonly_fields = ["item_number", "line_total","source_object"]


@admin.register(DeliveryOrder)
class DeliveryOrderAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["number", "get_status_label", "client", "date", "total_amount", "get_invoiced_label"]
    list_filter = [ ("status",MultipleChoicesDropdownFilter), ("date",RangeDateFilter)]
    search_fields = ["number", "external_id", "client__name", "shipment_number", "shipping_agency__name"]
    autocomplete_fields = ["company", "client", "sales_invoice", "configuration", "project_file", "shipping_agency"]
    inlines = [DeliveryOrderItemInline, DeliveryOrderAppliedTaxInline, DeliveryOrderIdentityCodeInline, DeliveryOrderDocumentInline, EmailLogInline]
    readonly_fields = ["number", "date", "subtotal_amount", "discount_amount", "tax_amount", "total_amount"]
    list_filter_submit = True

    fieldsets = (
        (_("General"), {
            "classes": ["tab"],
            "fields": (
                ("number", "date"),
                ("company", "client"),
                ("project_file", "external_id"),
                ("status", "subtotal_amount"),
                ("discount_amount", "tax_amount", "total_amount"),
                "sales_invoice",
                "notes",
            )
        }),
        (_("Shipping"), {
            "classes": ["tab"],
            "fields": (
                "delivery_address",
                ("package_count", "shipping_agency"),
                ("shipment_number", "tracking_url"),
            )
        }),
    )

    actions_row = ["create_invoice_from_do_row_action", "export_delivery_order_action", "send_email_row_action"]
    actions_detail = [
        "create_invoice_from_do_detail_action",
        "export_delivery_order_detail_action",
        "send_email_detail_action",
        "link_identity_codes_detail_action",
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
            _(DeliveryOrderStatus.DRAFT): "warning",
            _(DeliveryOrderStatus.DELIVERED): "success",
            _(DeliveryOrderStatus.SENT): "info",
            _(DeliveryOrderStatus.CANCELLED): "danger",
        },
    )
    def get_status_label(self, obj):
        return obj.status

    @display(
        description=_("Invoiced"),
        label={
            _("Yes"): "success",
            _("No"): "warning",
        },
    )
    def get_invoiced_label(self, obj):
        return _("Yes") if obj.sales_invoice_id else _("No")

    def save_related(self, request, form, formsets, change):
        from invoices.services.delivery_order_service import DeliveryOrderService

        super().save_related(request, form, formsets, change)
        delivery_order = form.instance

        # Editing the goods lines changes what the carried quote discount and
        # the taxes were computed on, so they are re-derived from the sources —
        # otherwise the financing line and the tax bases stay at their original
        # amounts and the document (and any invoice made from it) is left
        # inconsistent. Only self-contained delivery orders carry that data;
        # legacy ones are priced net of the discount and must be left alone.
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
        from invoices.services.delivery_order_service import DeliveryOrderService
        from django.utils import timezone
        from django.contrib import messages as django_messages

        do = DeliveryOrder.objects.select_related("client", "company", "project_file").get(pk=object_id)

        if do.sales_invoice_id:
            django_messages.warning(request, _("This delivery order is already linked to an invoice."))
            return redirect(reverse("admin:sales_deliveryorder_changelist"))

        try:
            invoice = DeliveryOrderService.create_invoice_from_delivery_orders(
                delivery_orders=[do],
                company=do.company,
                client=do.client,
                date_issued=timezone.now().date(),
            )
            django_messages.success(
                request,
                _("Invoice created from Delivery Order %(number)s.") % {"number": do.number},
            )
            return redirect(reverse("admin:invoices_salesinvoice_change", args=[invoice.id]))
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


@admin.register(DeliveryOrderItem)
class DeliveryOrderItemAdmin(ModelAdmin):
    list_display = ["item_number", "description", "quantity", "delivery_order"]
    search_fields = ["item_number", "description", "delivery_order__number"]
    autocomplete_fields = ["delivery_order"]
    readonly_fields = ["item_number", "line_total"]
    inlines = [IdentityCodeDeliveryInline]
