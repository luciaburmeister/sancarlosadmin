from django.contrib import admin
from unfold.admin import ModelAdmin
from accounting.models import ChartOfAccountsTemplate, TemplateAccount, TemplateType

@admin.register(ChartOfAccountsTemplate)
class ChartOfAccountsTemplateAdmin(ModelAdmin):
    list_display = ["name", "template_type", "country", "is_default"]
    list_filter = ["template_type", "country", "is_default"]
    search_fields = ["name"]
    ordering = ["country", "name"]


@admin.register(TemplateAccount)
class TemplateAccountAdmin(ModelAdmin):
    list_display = ["code", "name", "template", "parent", "is_postable", "role"]
    list_filter = ["template", "is_postable", "role"]
    search_fields = ["code", "name"]
    ordering = ["template", "code"]
    list_select_related = ["template", "parent"]
