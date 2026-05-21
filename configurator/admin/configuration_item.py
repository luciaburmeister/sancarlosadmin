from django.contrib import admin
from unfold.admin import ModelAdmin
from import_export.admin import ImportExportModelAdmin
from configurator.models import ConfigurationItem
from sales.resources import ConfigurationItemImportResource
from sales.forms import ConfigurationItemImportForm, ConfigurationItemConfirmImportForm
from core.admin.company_access import get_user_companies


@admin.register(ConfigurationItem)
class ConfigurationItemAdmin(ImportExportModelAdmin, ModelAdmin):
    resource_classes = [ConfigurationItemImportResource]
    import_form_class = ConfigurationItemImportForm

    list_display = (
        "configuration",
        "component",
        "quantity",
        "unit_price",
        "line_total",
        "created_at",
    )

    list_filter = ("created_at", "component")
    search_fields = (
        "component__name", "component__code",
        "configuration__name", "configuration__quote__project_name",
    )
    autocomplete_fields = ["configuration", "component"]
    readonly_fields = ("line_total", "label", "created_at")

    fieldsets = (
        ("Item", {
            "fields": ("configuration", "component", "quantity", "unit_price", "line_total"),
        }),
        ("Timestamps", {
            "fields": ("created_at",),
            "classes": ("collapse",),
        }),
    )

    def get_import_form_kwargs(self, request):
        kwargs = super().get_import_form_kwargs(request)
        kwargs["companies"] = get_user_companies(request.user)
        kwargs["request"] = request
        return kwargs

    def get_confirm_form_class(self, request):
        return ConfigurationItemConfirmImportForm

    def get_confirm_form_initial(self, request, import_form):
        initial = super().get_confirm_form_initial(request, import_form)
        if import_form and hasattr(import_form, "cleaned_data"):
            company = import_form.cleaned_data.get("company")
            if company:
                initial["company"] = str(company.pk)
        return initial

    def get_import_resource_kwargs(self, request, **kwargs):
        from company.models import Company as CompanyModel
        result = super().get_import_resource_kwargs(request, **kwargs)
        form = kwargs.get("form")
        company = None
        if form and hasattr(form, "cleaned_data"):
            raw = form.cleaned_data.get("company")
            if raw:
                if hasattr(raw, "pk"):
                    company = raw
                else:
                    try:
                        company = CompanyModel.objects.get(pk=raw)
                    except CompanyModel.DoesNotExist:
                        pass
        if company:
            result["default_company"] = company
        else:
            companies = get_user_companies(request.user)
            if companies.count() == 1:
                result["default_company"] = companies.first()
        return result
