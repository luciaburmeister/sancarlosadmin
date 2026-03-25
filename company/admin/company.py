from django.contrib import admin
from unfold.admin import ModelAdmin
from unfold.decorators import action # Added import


from company.models import Company, CompanyUser, CompanyEmailConfig, BankAccount
from django.utils.translation import gettext_lazy as _
from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from core.admin.company_access import get_user_companies
from unfold.admin import ModelAdmin, StackedInline

from unfold.contrib.forms.widgets import ArrayWidget
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
    

@admin.register(Company)
class CompanyAdmin(ModelAdmin):
    inlines = [BankAccountInline]
    list_display = ("name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)

    ordering = ("name",)

    fieldsets = (
        (None, {
            "fields": (
                "name",
                "legal_name",
                "is_active",
                "logo",
                "address",
                "email",
                "phone",
                "tax_identifier",
                "constituted_country",
                "production_stages",
                "asset_identifier_labels",
                "business_activity_description"
            )
        }),
    )

    formfield_overrides = {
        ArrayField: {
            "widget": ArrayWidget,
        }
    }
    
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
