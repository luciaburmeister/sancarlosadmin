from django.contrib import admin
from django.contrib.postgres.fields import ArrayField
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, StackedInline
from unfold.admin import GenericTabularInline as _GenericTabularInline
from unfold.decorators import display
from unfold.contrib.forms.widgets import ArrayWidget
from unfold.widgets import UnfoldAdminTextareaWidget
from unfold.contrib.import_export.forms import SelectableFieldsExportForm
from import_export.admin import ImportExportModelAdmin

from core.admin.company_admin import CompanyRestrictedAdmin
from core.admin.email_log import EmailLogInline
from invoices.models import AppliedTax, SalesInvoice, SalesInvoiceStatus
from payments.admin_mixins import OutstandingBalanceAdminMixin, PendingInvoicesSection
from payments.mixins import currency_symbol
from payments.models import PaymentDirection
from sales.models import Client, ClientAddress, ClientContact, ClientBankAccount
from sales.forms import ClientImportForm, ClientConfirmImportForm
from sales.resources import ClientResource

from .mixins import _CompanyImportMixin


@admin.register(ClientBankAccount)
class ClientBankAccountAdmin(CompanyRestrictedAdmin,ModelAdmin):
    list_display = ["bank_name", "account_number", "client"]
    search_fields = ["bank_name", "account_number", "iban"]
    autocomplete_fields = ["client"]

@admin.register(ClientAddress)
class ClientAddressAdmin(CompanyRestrictedAdmin,ModelAdmin):
    list_display = ["name", "street", "postal_code", "locality", "city", "province", "country", "contact_name", "contact_phone", "client"]
    search_fields = ["name", "street", "city", "locality", "postal_code", "province", "country", "contact_name", "contact_phone", "client__name"]
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
                "name",
                "street",
                ("postal_code", "locality", "city"),
                ("province", "country"),
                ("contact_name", "contact_phone"),
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
                ("sepa_mandate_reference", "sepa_mandate_signed_on"),
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
class ClientAdmin(_CompanyImportMixin, OutstandingBalanceAdminMixin, CompanyRestrictedAdmin, ImportExportModelAdmin, ModelAdmin):
    resource_classes = [ClientResource]
    import_form_class = ClientImportForm
    export_form_class = SelectableFieldsExportForm

    balance_invoice_model = SalesInvoice
    balance_counterparty_field = "client"
    balance_invoice_filters = {"status": SalesInvoiceStatus.FINAL}
    credit_payment_field = "client"
    credit_payment_direction = PaymentDirection.INBOUND

    list_display = ["name", "commercial_name", "tax_identifier", "phone", "balance_overdue_display", "balance_not_due_display", "balance_owed_display", "credit_balance_display"]
    list_sections = [PendingInvoicesSection]
    search_fields = [
        "name", "commercial_name", "tax_identifier", "industry_identifier", "nima", "email", "phone", "address",
        "contacts__name", "contacts__email", "contacts__phone", "contacts__mobile_phone", "contacts__department",
    ]
    list_filter = ["sale_zone", "is_insured", "contacts__role"]
    autocomplete_fields = ["company", "agent", "sale_zone", "our_bank_account","discount_policies","default_client_bank_account","account"]
    list_select_related = ["company"]   

    def get_list_filter(self, request):
        return self.list_filter

    @display(description=_("Balance owed (they owe us)"))
    def balance_owed_detail_display(self, obj):
        if not obj or not obj.pk:
            return "—"
        amount = getattr(obj, "balance_owed", None)
        if not amount:
            return "—"
        symbol = currency_symbol(getattr(obj, "balance_currency", "EUR"))
        return format_html(
            '<span style="color:#16a34a;font-weight:600;">{} {}</span> '
            '<span style="margin-left:10px;color:#6b7280;">'
            '{}: <span style="color:#dc2626;font-weight:600;">{} {}</span> · {}: {} {}</span> '
            '<a href="{}" target="_blank" rel="noopener" '
            'style="display:inline-block;margin-left:10px;padding:4px 12px;border-radius:6px;'
            'background:#16a34a;color:#fff;font-weight:600;text-decoration:none;white-space:nowrap;">'
            '{}</a>',
            amount, symbol,
            _("Overdue"), getattr(obj, "balance_overdue", 0) or 0, symbol,
            _("Not overdue"), getattr(obj, "balance_not_due", 0) or 0, symbol,
            self._pending_invoices_url(obj), _("See pending invoices"),
        )

    @display(description=_("Credit (they paid, not yet allocated)"))
    def credit_balance_detail_display(self, obj):
        if not obj or not obj.pk:
            return "—"
        amount = getattr(obj, "credit_balance", None)
        if not amount:
            return "—"
        symbol = currency_symbol(getattr(obj, "credit_balance_currency", "EUR"))
        return format_html(
            '<span style="color:#16a34a;font-weight:600;">{} {}</span> '
            '<a href="{}" target="_blank" rel="noopener" '
            'style="display:inline-block;margin-left:10px;padding:4px 12px;border-radius:6px;'
            'background:#16a34a;color:#fff;font-weight:600;text-decoration:none;white-space:nowrap;">'
            '{}</a>',
            amount, symbol,
            self._unallocated_payments_url(obj), _("See unallocated payments"),
        )

    def get_confirm_form_class(self, request):
        return ClientConfirmImportForm

    inlines = [
        ClientAddressInline,
        ClientContactInline,
        ClientBankAccountInline,
        ClientAppliedTaxInline,
        EmailLogInline,
    ]
    formfield_overrides = {
        ArrayField: {
            "widget": ArrayWidget,
        }
    }

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "payment_terms_observations":
            kwargs["widget"] = UnfoldAdminTextareaWidget(attrs={"rows": 4})
        return super().formfield_for_dbfield(db_field, request, **kwargs)

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
        label={True: "success", False: "danger"},
    )
    def has_signed_terms(self, obj):
        signed = bool(obj.terms_signature)
        return signed, _("Sí") if signed else _("No")

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
                ("tax_identifier", "industry_identifier", "nima"),
                ("phone", "email"),
                "balance_owed_detail_display",
                "credit_balance_detail_display",
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
                ("payment_method", "payment_terms_days", "payment_days"),
                "payment_terms_observations",
                ("our_bank_account", "default_client_bank_account"),
                "account",
            )
        }),
        (_("Payment Reminders"), {
            "classes": ["tab"],
            "description": _(
                "Controls whether this client receives automatic payment reminder "
                "emails, to which addresses, and in what language. Reminders can be "
                "turned off entirely for this client here, or for a single invoice "
                "from that invoice's own record."
            ),
            "fields": (
                "language",
                "exclude_from_payment_reminders",
                "payment_reminder_emails",
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
        "credit_balance_detail_display",
    ]

    @display(description=_("Versión T&C"))
    def terms_version_display(self, obj):
        return obj.terms_version or "—"
