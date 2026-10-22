from django.apps import apps
from django.contrib import admin
from django.contrib.contenttypes.models import ContentType
from unfold.admin import ModelAdmin
from core.admin.company_admin import CompanyRestrictedAdmin
from accounting.models import AllocationAccountRule


def configurable_document_types():
    """Documents payments can be allocated to whose booking isn't fixed by a strategy in code."""
    from accounting.services.payment_journal import strategy_for
    from payments.mixins import Payable

    models = [
        model for model in apps.get_models()
        if issubclass(model, Payable) and not strategy_for(model)
    ]
    return ContentType.objects.get_for_models(*models).values()


@admin.register(AllocationAccountRule)
class AllocationAccountRuleAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ["content_type", "account", "company"]
    list_filter = ["company"]
    search_fields = ["account__code", "account__name"]
    list_select_related = ["company", "content_type", "account"]
    autocomplete_fields = ["company", "account"]
    fields = ["company", "content_type", "account"]

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "content_type":
            kwargs["queryset"] = ContentType.objects.filter(
                pk__in=[content_type.pk for content_type in configurable_document_types()],
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)
