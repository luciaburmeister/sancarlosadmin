from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline, StackedInline
from core.admin.company_admin import CompanyRestrictedAdmin
from simple_history.admin import SimpleHistoryAdmin
from django.db import models
from django.utils.translation import gettext_lazy as _
from django_json_widget.widgets import JSONEditorWidget
from assets.models import Asset, AssetIdentifier, AssetTransaction
from unfold.widgets import UnfoldAdminSelectWidget
from django import forms
from django.urls import resolve

class AssetIdentifierInline(TabularInline):
    model = AssetIdentifier
    extra = 1
    tab=True

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == "label":
            try:
                resolved = resolve(request.path_info)
                object_id = resolved.kwargs.get("object_id")
                if object_id:
                    asset = Asset.objects.select_related("company").get(pk=object_id)
                    labels = asset.company.asset_identifier_labels
                    if labels:
                        kwargs["widget"] = UnfoldAdminSelectWidget(
                            choices=[("", "---------")] + [(l, l) for l in labels]
                        )
            except Exception:
                pass
        return super().formfield_for_dbfield(db_field, request, **kwargs)

        return super().formfield_for_dbfield(db_field, request, **kwargs)

class AssetTransactionInline(StackedInline):
    model = AssetTransaction
    extra = 0
    tab = True
    readonly_fields = ("created_at",)
    ordering = ("-date",)
    fieldsets = (
        (
            _("Transaction"),
            {
                "fields": (
                    ("transaction_type", "date"),
                    ("from_zone", "to_zone"),
                    "notes",
                )
            },
        ),
    )

@admin.register(Asset)
class AssetAdmin(CompanyRestrictedAdmin, SimpleHistoryAdmin, ModelAdmin):
    list_display = ("name", "asset_type", "client", "get_identifiers")
    list_filter = ("asset_type", "client")
    search_fields = ("name", "identifiers__value")
    inlines = [AssetIdentifierInline, AssetTransactionInline]
    formfield_overrides = {
        models.JSONField: {"widget": JSONEditorWidget},
    }
    autocomplete_fields = ["asset_type", "client","company","current_working_zone"]
    fieldsets = (
        (
            _("Asset"),
            {
                "fields": (
                    ("name", "asset_type"),
                    ("client", "company"),
                    ("current_working_zone"),
                    "data",
                )
            },
        ),
    )

    def get_identifiers(self, obj):
        return ", ".join([f"{i.label}: {i.value}" for i in obj.identifiers.all()])
    get_identifiers.short_description = _("Identifiers")
