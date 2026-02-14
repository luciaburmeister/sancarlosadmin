from django.contrib import admin
from unfold.admin import ModelAdmin
from core.admin.company_admin import CompanyRestrictedAdmin
from django.db import models
from django_json_widget.widgets import JSONEditorWidget
from ..models import AssetType

@admin.register(AssetType)
class AssetTypeAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("name", "description")
    search_fields = ("name",)
    formfield_overrides = {
        models.JSONField: {"widget": JSONEditorWidget},
    }
