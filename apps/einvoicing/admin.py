from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy as _
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action, display

from apps.einvoicing.models import (
    EInvoicingAccount,
    EInvoicingSubmission,
    SubmissionStatus,
)
from apps.einvoicing.services import EInvoicingService
from core.admin.company_admin import CompanyRestrictedAdmin


# ── Account admin ────────────────────────────────────────────────────────


class EInvoicingAccountForm(forms.ModelForm):
    """Custom form that provides a plain-text 'api_key' field for input."""

    api_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        label=_("API Key"),
        help_text=_("Enter a new API key to replace the stored one. Leave blank to keep the current key."),
    )

    class Meta:
        model = EInvoicingAccount
        fields = "__all__"
        exclude = ("api_key_encrypted",)

    def save(self, commit=True):
        instance = super().save(commit=False)
        new_key = self.cleaned_data.get("api_key", "").strip()
        if new_key:
            instance.set_api_key(new_key)
        if commit:
            instance.save()
            self.save_m2m()
        return instance


@admin.register(EInvoicingAccount)
class EInvoicingAccountAdmin(CompanyRestrictedAdmin, ModelAdmin):
    form = EInvoicingAccountForm
    list_display = ("name", "company", "provider", "invoice_serie", "is_sandbox", "is_active")
    list_filter = ("provider", "is_sandbox", "is_active", "company")
    search_fields = ("name",)
    autocomplete_fields = ("company",)
    actions_row = ["test_connection_action"]

    fieldsets = (
        (_("Account"), {
            "fields": (
                ("name", "company"),
                "provider",
                "api_key",
            ),
        }),
        (_("Configuration"), {
            "fields": (
                "invoice_serie",
                "base_url",
                ("is_sandbox", "is_active"),
            ),
        }),
    )

    @action(description=_("Test Connection"), icon="wifi")
    def test_connection_action(self, request, object_id):
        account = EInvoicingAccount.objects.get(pk=object_id)
        healthy = EInvoicingService.check_account_health(account)
        if healthy:
            messages.success(request, _("Connection successful! API key is valid."))
        else:
            messages.error(request, _("Connection failed. Check the API key and base URL."))
        return redirect(request.META.get("HTTP_REFERER", ".."))


# ── Submission admin ─────────────────────────────────────────────────────


@admin.register(EInvoicingSubmission)
class EInvoicingSubmissionAdmin(ModelAdmin):
    list_display = (
        "sales_invoice",
        "action",
        "get_status_label",
        "account",
        "submitted_at",
        "created_at",
    )
    list_filter = ("status", "action", "account__company")
    search_fields = (
        "sales_invoice__invoice_number",
        "provider_record_id",
    )
    readonly_fields = (
        "account",
        "sales_invoice",
        "action",
        "status",
        "request_payload",
        "response_payload",
        "provider_record_id",
        "error_message",
        "submitted_at",
        "created_at",
    )

    fieldsets = (
        (_("Submission"), {
            "fields": (
                ("account", "sales_invoice"),
                ("action", "status"),
                ("submitted_at", "created_at"),
                "provider_record_id",
                "error_message",
            ),
        }),
        (_("Payloads"), {
            "fields": (
                "request_payload",
                "response_payload",
            ),
            "classes": ("collapse",),
        }),
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @display(
        description=_("Status"),
        ordering="status",
        label={
            _("Accepted"): "success",
            _("Submitted"): "info",
            _("Pending"): "warning",
            _("Rejected"): "danger",
            _("Error"): "danger",
        },
    )
    def get_status_label(self, obj):
        return _(obj.get_status_display())


# ── Inline for SalesInvoiceAdmin ─────────────────────────────────────────


class EInvoicingSubmissionInline(TabularInline):
    model = EInvoicingSubmission
    extra = 0
    tab = True
    tab_title = _("AEAT Submissions")
    fields = ("action", "status", "provider_record_id", "error_message", "submitted_at")
    readonly_fields = ("action", "status", "provider_record_id", "error_message", "submitted_at")
    can_delete = False
    ordering = ("-created_at",)

    def has_add_permission(self, request, obj=None):
        return False
