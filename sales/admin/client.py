from django.contrib import admin
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, StackedInline
from unfold.admin import GenericTabularInline as _GenericTabularInline
from unfold.decorators import display
from import_export.admin import ImportExportModelAdmin

from django.urls import reverse

from core.admin.company_admin import CompanyRestrictedAdmin
from invoices.models import AppliedTax, SalesInvoice, SalesInvoiceStatus
from payments.mixins import annotate_outstanding_balance
from sales.models import Client, ClientAddress, ClientContact, ClientBankAccount
from sales.forms import ClientImportForm, ClientExportForm, ClientConfirmImportForm
from sales.resources import ClientResource

from .mixins import _CompanyImportMixin


@admin.register(ClientBankAccount)
class ClientBankAccountAdmin(CompanyRestrictedAdmin,ModelAdmin):
    list_display = ["bank_name", "account_number", "client"]
    search_fields = ["bank_name", "account_number", "iban"]
    autocomplete_fields = ["client"]

@admin.register(ClientAddress)
class ClientAddressAdmin(CompanyRestrictedAdmin,ModelAdmin):
    list_display = ["street", "city", "province", "country", "client"]
    search_fields = ["street", "city", "postal_code", "province", "country", "client__name"]
    autocomplete_fields = ["client"]

class ClientAddressInline(StackedInline):
    model = ClientAddress
    extra = 0
    tab = True
    tab_title = _("Addresses")
    fieldsets = (
        (None, {
            "fields": (
                ("address_type", "is_default"),
                "country",
                ("province", "city", "postal_code"),
                "street",
            ),
        }),
    )

class ClientContactInline(StackedInline):
    model = ClientContact
    extra = 0
    tab = True
    tab_title = _("Contacts")
    autocomplete_fields = ["address"]
    fieldsets = (
        (None, {
            "fields": (
                ("name", "position"),
                ("role", "department"),
                ("email", "phone", "mobile_phone"),
                ("preferred_contact_method", "address"),
                "notes",
            ),
        }),
    )

class ClientBankAccountInline(StackedInline):
    model = ClientBankAccount
    extra = 0
    tab = True
    tab_title = _("Bank Accounts")
    fieldsets = (
        (None, {
            "fields": (
                ("bank_name", "holder_name"),
                ("iban", "swift_bic"),
                ("account_number", "routing_number"),
                ("country", "is_default"),
            ),
        }),
    )

class ClientAppliedTaxInline(_GenericTabularInline):
    model = AppliedTax
    extra = 0
    autocomplete_fields = ("tax_type",)
    ordering = ("order",)
    fields = ("tax_type", "selected_percentage", "order")
    verbose_name = _("Default tax")
    verbose_name_plural = _("Default taxes")
    tab = True

@admin.register(Client)
class ClientAdmin(_CompanyImportMixin, CompanyRestrictedAdmin, ImportExportModelAdmin, ModelAdmin):
    resource_classes = [ClientResource]
    import_form_class = ClientImportForm
    export_form_class = ClientExportForm

    list_display = ["name", "commercial_name", "tax_identifier", "industry_identifier", "phone", "address_truncated",  "has_signed_terms", "balance_owed_display"]
    search_fields = [
        "name", "commercial_name", "tax_identifier", "industry_identifier", "email", "phone", "address",
        "contacts__name", "contacts__email", "contacts__phone", "contacts__mobile_phone", "contacts__department",
    ]
    list_filter = ["sale_zone", "is_insured", "contacts__role"]
    autocomplete_fields = ["company", "agent", "sale_zone", "our_bank_account","discount_policies","default_client_bank_account"]
    


    def get_list_filter(self, request):
        return self.list_filter

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return annotate_outstanding_balance(
            qs, SalesInvoice, "client", "balance_owed",
            invoice_filters={"status": SalesInvoiceStatus.FINAL},
        )

    @staticmethod
    def _pending_invoices_url(obj):
        return (
            reverse("admin:invoices_salesinvoice_changelist")
            + f"?client__id__exact={obj.pk}&payment_status__in=unpaid,partial"
            + f"&status__exact={SalesInvoiceStatus.FINAL}"
        )

    @display(description=_("Balance owed"))
    def balance_owed_display(self, obj):
        if not obj or not obj.pk:
            return "—"
        amount = getattr(obj, "balance_owed", None)
        if not amount:
            return "—"
        return format_html(
            '<a href="{}" target="_blank" rel="noopener" style="color:#16a34a;font-weight:600;">{} €</a>',
            self._pending_invoices_url(obj), amount,
        )
    balance_owed_display.admin_order_field = "balance_owed"

    @display(description=_("Balance owed (they owe us)"))
    def balance_owed_detail_display(self, obj):
        if not obj or not obj.pk:
            return "—"
        amount = getattr(obj, "balance_owed", None)
        if not amount:
            return "—"
        return format_html(
            '<span style="color:#16a34a;font-weight:600;">{} €</span> '
            '<a href="{}" target="_blank" rel="noopener" '
            'style="display:inline-block;margin-left:10px;padding:4px 12px;border-radius:6px;'
            'background:#16a34a;color:#fff;font-weight:600;text-decoration:none;white-space:nowrap;">'
            '{}</a>',
            amount, self._pending_invoices_url(obj), _("See pending invoices"),
        )

    def get_confirm_form_class(self, request):
        return ClientConfirmImportForm

    @display(description=_("Address"))
    def address_truncated(self, obj):
        if not obj.address or obj.address == "-":
            return "-"
        return obj.address if len(obj.address) <= 40 else obj.address[:40] + "…"

    inlines = [ClientAddressInline, ClientContactInline, ClientBankAccountInline, ClientAppliedTaxInline]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "default_client_bank_account":
            import re
            match = re.search(r'/admin/sales/client/(\d+)/change/', request.path)
            if match:
                client_id = match.group(1)
                kwargs["queryset"] = ClientBankAccount.objects.filter(client_id=client_id)
            else:
                kwargs["queryset"] = ClientBankAccount.objects.none()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @display(
        description=_("T&C firmados"),
        label={
            _("Sí"): "success",
            _("No"): "danger",
        },
    )
    def has_signed_terms(self, obj):
        return _("Sí") if obj.terms_signature else _("No")

    @display(description=_("Firma"))
    def terms_signature_preview(self, obj):
        if obj.terms_signature:
            return format_html(
                '<img src="{}" style="width:400px;height:150px;object-fit:contain;border:1px solid #ccc;background:white;" />',
                obj.terms_signature.url,
            )
        return _("Sin firma")

    fieldsets = (
        (_("Basic Information"), {
            "classes": ["tab"],
            "fields": (
                ("name", "commercial_name", ),
                ("company",),
                ("tax_identifier", "industry_identifier"),
                ("phone", "email"),
                "balance_owed_detail_display",
                "address"
            )
        }),
        (_("Sales & Territory"), {
            "classes": ["tab"],
            "fields": (
                ("sale_zone", "agent"),
                "discount_policies",
            )
        }),
        (_("Financial & Insurance"), {
            "classes": ["tab"],
            "fields": (
                ("is_insured", "max_risk", "current_risk"),
                ("payment_method", "payment_terms_days", "our_bank_account"),
                "default_client_bank_account",
            )
        }),
        (_("Payment Reminders"), {
            "classes": ["tab"],
            "description": _(
                "Controls whether this client receives automatic payment reminder "
                "emails, and in what language. Reminders can be turned off entirely "
                "for this client here, or for a single invoice from that invoice's "
                "own record."
            ),
            "fields": (
                "language",
                "exclude_from_payment_reminders",
            )
        }),
        (_("Términos y Condiciones"), {
            "classes": ["tab"],
            "fields": (
                "has_signed_terms",
                "terms_accepted_at",
                "accepted_terms_company",
                "terms_version_display",
                "terms_signature_preview",
            )
        }),
    )
    readonly_fields = [
        "address",
        "has_signed_terms",
        "terms_accepted_at",
        "accepted_terms_company",
        "terms_version_display",
        "terms_signature_preview",
        "balance_owed_detail_display",
    ]

    @display(description=_("Versión T&C"))
    def terms_version_display(self, obj):
        return obj.terms_version or "—"