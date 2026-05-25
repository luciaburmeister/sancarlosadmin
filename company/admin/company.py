from django.contrib import admin
from django.forms.models import BaseInlineFormSet
from django.forms import ModelForm
from django.core.exceptions import ValidationError
from unfold.admin import ModelAdmin
from unfold.decorators import action # Added import


from company.models import Company, CompanyScheduleSlot, CompanyUser, CompanyEmailConfig, BankAccount, CompanyDocument, CompanyDocumentType
from django.utils.translation import gettext_lazy as _
from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from core.admin.company_access import get_user_companies
from unfold.admin import ModelAdmin, StackedInline
from core.admin.company_admin import CompanyRestrictedAdmin
from company.forms import CompanyDocumentForm

from unfold.contrib.forms.widgets import ArrayWidget, WysiwygWidget
from unfold.widgets import UnfoldAdminSelectWidget, UnfoldAdminTimeWidget
from django.contrib.postgres.fields import ArrayField

@admin.register(BankAccount)
class BankAccountAdmin(ModelAdmin):
    list_display = ("bank_name", "account_number", "company")
    search_fields = ("bank_name", "account_number", "iban")
    autocomplete_fields = ("company",)

class BankAccountInline(StackedInline):
    model = BankAccount
    extra = 1
    fields = ("bank_name", "holder_name", "account_number", "iban", "swift_bic", "routing_number", "is_default")
    tab = True
    tab_title = _("Bank Accounts")

class CompanyDocumentInline(StackedInline):
    model = CompanyDocument
    form = CompanyDocumentForm
    extra = 0
    tab = True
    tab_title = _("Documents")
    autocomplete_fields = ["document_type"]

    fieldsets = (
        (None, {
            "fields": (
                ("document_type",  "file"),
                ("does_not_expire","is_active"),
                ("issue_date", "expiry_date"),
                "notes",
            )
        }),
    )

@admin.register(CompanyDocumentType)
class CompanyDocumentTypeAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("name", "company")
    search_fields = ("name",)
    autocomplete_fields = ["company"]

class CompanyScheduleSlotForm(ModelForm):
    class Meta:
        model = CompanyScheduleSlot
        fields = "__all__"
        widgets = {
            "weekday": UnfoldAdminSelectWidget,
            "start_time": UnfoldAdminTimeWidget,
            "end_time": UnfoldAdminTimeWidget,
        }

class CompanyScheduleSlotInlineFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()

        slots_by_day = {}

        for form in self.forms:
            if not hasattr(form, "cleaned_data"):
                continue

            if form.cleaned_data.get("DELETE"):
                continue

            weekday = form.cleaned_data.get("weekday")
            start_time = form.cleaned_data.get("start_time")
            end_time = form.cleaned_data.get("end_time")

            if weekday is None or start_time is None or end_time is None:
                continue

            if end_time <= start_time:
                raise ValidationError(_("End time must be after start time."))

            slots_by_day.setdefault(weekday, []).append((start_time, end_time))

        for weekday, slots in slots_by_day.items():
            slots.sort(key=lambda x: x[0])

            for i in range(1, len(slots)):
                prev_start, prev_end = slots[i - 1]
                curr_start, curr_end = slots[i]

                if curr_start < prev_end:
                    raise ValidationError(
                        _("There are overlapping slots on the same weekday.")
                    )

class CompanyScheduleSlotInline(admin.TabularInline):
    model = CompanyScheduleSlot
    form = CompanyScheduleSlotForm
    formset = CompanyScheduleSlotInlineFormSet
    extra = 0
    tab = True
    fields = ("weekday", "start_time", "end_time")
    ordering = ("weekday", "start_time")

@admin.register(Company)
class CompanyAdmin(ModelAdmin):
    inlines = [BankAccountInline, CompanyDocumentInline]
    list_display = ("name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)
    ordering = ("name",)

    inlines = [CompanyScheduleSlotInline]

    fieldsets = (
        (_("Base Info"), {
            "classes": ["tab"],
            "fields": (
                ("name",
                "legal_name"),
                ("logo",
                "constituted_country"),
                "address",
                ("email",
                "phone"),
                "is_active"

            )
        }),
        (_("Application Configurations"), {
            "fields": (

                ("production_stages",
                "asset_identifier_labels"),
                "allow_shared_production_stage_activities",
                ("document_reminder_emails"),
            ),
            "classes": ["tab"]
        }),
        (_("Taxes"), {
            "fields": (
                "tax_identifier",
                "business_activity_description"
            ),
            "classes": ["tab"]
        }),
        (_("Términos y Condiciones"), {
            "fields": ("terms_text",),
            "classes": ["tab"],
            "description": "Texto que el cliente firmará digitalmente al registrarse en la app. Déjalo vacío para no requerir firma.",
        }),
    )

    formfield_overrides = {
        ArrayField: {
            "widget": ArrayWidget,
        }
    }

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "terms_text":
            kwargs["widget"] = WysiwygWidget
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    actions_row = ["install_chart_action"]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.filter(id__in=get_user_companies(request.user))

    @action(
        description=_("Install Chart"),
        url_path="install-chart",
        icon="playlist_add"
    )
    def install_chart_action(self, request, object_id: int):
        from accounting.services.chart_installer import install_chart_standard
        
        company = get_object_or_404(Company, pk=object_id)
        
        if not hasattr(company, 'chart'):
            messages.error(request, _("Company has no Chart of Accounts template assigned."))
            return redirect(request.META.get("HTTP_REFERER"))
            
        try:
            # We assume company.chart.template exists if company.chart exists
            count = install_chart_standard(company, company.chart.template)
            messages.success(request, _(f"Successfully installed {count} accounts from template."))
        except Exception as e:
            messages.error(request, _(f"Error installing chart: {str(e)}"))
            
        return redirect(request.META.get("HTTP_REFERER"))


@admin.register(CompanyUser)
class CompanyUserAdmin(ModelAdmin):
    list_display = ("user", "company", "is_active")
    list_filter = ("company", "is_active")
    search_fields = (
        "user__username",
        "user__email",
        "company__name",
    )

    autocomplete_fields = ("user", "company")

    ordering = ("company", "user")

@admin.register(CompanyEmailConfig)
class CompanyEmailConfigAdmin(ModelAdmin):
    list_display = ("email_address", "company", "imap_server", "last_checked")
    list_filter = ("company", "use_ssl")
    search_fields = ("email_address", "imap_server", "imap_user")
    autocomplete_fields = ("company",)
    actions_row = ["pull_emails_action"]

    @action(
        description=_("pull emails"),
        url_path="pull-emails",
        icon="mail"
    )
    def pull_emails_action(self, request, object_id: int):
        from invoices.tasks import fetch_email_invoices_task
        try:
            fetch_email_invoices_task.delay(object_id)
            messages.success(request, _("Email fetching task has been triggered in the background."))
        except Exception as e:
            messages.error(request, _(f"Error triggering task: {str(e)}"))
            
        return redirect(request.META.get("HTTP_REFERER"))
