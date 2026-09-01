from unfold.contrib.filters.admin import AutocompleteSelectMultipleFilter
from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline, StackedInline
from core.admin.company_admin import CompanyRestrictedAdmin
from simple_history.admin import SimpleHistoryAdmin
from django.db import models
from django.utils.translation import gettext_lazy as _
from django_json_widget.widgets import JSONEditorWidget
from assets.models import Asset, AssetIdentifier, AssetTransaction, AssetDocument, AssetDocumentType, MachinePermission, AssetFaultType, AssetFault
from assets.forms import AssetForm, AssetAddForm, build_asset_data_form_fields, DATA_FIELD_PREFIX
from assets.resources import AssetResource
from unfold.widgets import UnfoldAdminSelectWidget
from django.shortcuts import redirect
from django.urls import resolve, path, reverse
from django.utils.html import format_html
from django.http import HttpResponse, HttpResponseRedirect
from assets.utils import generate_asset_qr_grid
from company.forms import BaseDocumentForm
from unfold.contrib.filters.admin import AutocompleteSelectFilter
from django.forms import Textarea
from import_export.admin import ImportExportModelAdmin
from unfold.contrib.import_export.forms import ImportForm, SelectableFieldsExportForm



class AssetDocumentForm(BaseDocumentForm):
    class Meta:
        model = AssetDocument
        fields = "__all__"


class AssetDocumentInline(StackedInline):
    model = AssetDocument
    form = AssetDocumentForm
    extra = 0
    tab = True
    exclude = ["company"]
    autocomplete_fields = ["document_type"]

    fieldsets = (
        (None, {
            "fields": (
                ("document_type", "file"),
                ("does_not_expire", "is_active"),
                ("issue_date", "expiry_date"),
                "notes",
            )
        }),
    )


@admin.register(AssetDocumentType)
class AssetDocumentTypeAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("name", "company")
    search_fields = ("name",)
    autocomplete_fields = ["company"]


@admin.register(AssetFaultType)
class AssetFaultTypeAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("name", "company")
    search_fields = ("name",)
    autocomplete_fields = ["company"]


class AssetFaultInline(TabularInline):
    model = AssetFault
    extra = 0
    tab = True
    fields = ("fault_type", "severity", "status", "description", "created_at")
    readonly_fields = ("created_at",)
    autocomplete_fields = ["fault_type"]
    exclude = ["company"]


class AssetIdentifierInline(TabularInline):
    model = AssetIdentifier
    extra = 1
    tab = True

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        # Only rendered on the change form, where the asset type is known:
        # type with labels -> restricted select; without labels -> free text
        # (clean() doesn't restrict either).
        if db_field.name == "label":
            labels = []
            try:
                resolved = resolve(request.path_info)
                object_id = resolved.kwargs.get("object_id")
                if object_id:
                    asset = Asset.objects.select_related(
                        "asset_type", "company"
                    ).get(pk=object_id)
                    labels = asset.asset_type.get_identifier_labels()
            except Exception:
                pass

            if labels:
                kwargs["widget"] = UnfoldAdminSelectWidget(
                    choices=[("", "---------")] + [(l, l) for l in labels]
                )
        return super().formfield_for_dbfield(db_field, request, **kwargs)


class AssetTransactionInline(StackedInline):
    model = AssetTransaction
    extra = 0
    tab = True
    readonly_fields = ("created_at",)
    ordering = ("-date",)
    autocomplete_fields = ["from_zone", "to_zone", "project_file"]
    fieldsets = (
        (
            _("Transaction"),
            {
                "fields": (
                    ("transaction_type", "date"),
                    ("from_zone", "to_zone"),
                    "project_file",
                    "data",
                    "notes",
                )
            },
        ),
    )


class MachinePermissionInline(TabularInline):
    model = MachinePermission
    extra = 1
    tab = True
    verbose_name = _("Authorized Employee")
    verbose_name_plural = _("Authorized Employees")
    fields = ("employee", "notes", "granted_at")
    readonly_fields = ("granted_at",)
    autocomplete_fields = ["employee"]
    exclude = ["company"]
    formfield_overrides = {
        models.TextField: {"widget": Textarea(attrs={"rows": 2, "cols": 30, "style": "border: 1px solid #ccc; border-radius: 4px; padding: 4px;"})},
    }

    def save_new_objects(self, form, commit=True):
        obj = super().save_new_objects(form, commit=False)
        return obj

    def save_model(self, request, obj, form, change):
        if obj.asset and obj.asset.company:
            obj.company = obj.asset.company
        super().save_model(request, obj, form, change)


@admin.register(Asset)
class AssetAdmin(CompanyRestrictedAdmin, SimpleHistoryAdmin, ModelAdmin, ImportExportModelAdmin):
    resource_classes = [AssetResource]
    import_form_class = ImportForm
    export_form_class = SelectableFieldsExportForm
    list_display = ("name", "asset_type", "client", "current_working_zone", "zone_client", "provider", "assigned_to", "qr_link", "get_identifiers", "is_active", "operational_status")
    list_filter = (("asset_type",AutocompleteSelectMultipleFilter), ("client",AutocompleteSelectMultipleFilter), ("provider",AutocompleteSelectMultipleFilter), ("assigned_to",AutocompleteSelectMultipleFilter),("company",AutocompleteSelectMultipleFilter),"is_active","operational_status")
    search_fields = ("name", "identifiers__value")
    inlines = [AssetIdentifierInline, AssetTransactionInline, AssetDocumentInline, MachinePermissionInline, AssetFaultInline]
    actions = ["download_qr_grid"]
    form = AssetForm
    list_editable = ("is_active",)
    list_filter_submit = True
    autocomplete_fields = [
        "asset_type",
        "client",
        "company",
        "current_working_zone",
        "assigned_to",
        "provider",
        "excluded_assets"]
    readonly_fields = ("qr_code_preview", "operational_status")
    list_select_related = ["asset_type","client","provider","assigned_to","current_working_zone","current_working_zone__client","company",]
    fieldsets = (
        (
            _("Asset"),
            {
                "fields": (
                    ("name", "asset_type"),
                    ("client", "company"),
                    ("assigned_to", "provider"),
                    ("current_working_zone", "is_active"),
                    "operational_status",
                    "description",
                )
            },
        ),
        (
            _("Compatibility"),
            {
                "fields": ("excluded_assets",),
            },
        ),
        (
            _("QR Code"),
            {
                "fields": ("qr_code_preview",),
            },
        ),
    )

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = AssetAddForm
            kwargs["fields"] = None
            return super().get_form(request, obj, **kwargs)
        dynamic_fields = build_asset_data_form_fields(obj.asset_type)
        kwargs["form"] = type(f"Dynamic{self.form.__name__}", (self.form,), dynamic_fields)
        return super().get_form(request, obj, **kwargs)

    def get_fieldsets(self, request, obj=None):
        fieldsets = list(super().get_fieldsets(request, obj))
        if obj is None:
            return fieldsets
        data_fields = [
            f"{DATA_FIELD_PREFIX}{spec.name}" for spec in obj.asset_type.fields.all()
        ]
        if data_fields:
            fieldsets.insert(
                1,
                (
                    _("%(type)s data") % {"type": obj.asset_type.name},
                    {"fields": data_fields},
                ),
            )
        return fieldsets

    def get_inlines(self, request, obj=None):
        # The add form is intentionally minimal: identifiers and type-specific
        # fields are filled on the change form, where the type is known.
        return [] if obj is None else super().get_inlines(request, obj)

    def get_urls(self):
        from assets.views.asset_wizard import AssetCreateWizardView
        from assets.views.asset_schedule import AssetScheduleCaptureView, AssetScheduleView

        urls = super().get_urls()
        custom = [
            path(
                "wizard/",
                self.admin_site.admin_view(
                    AssetCreateWizardView.as_view(model_admin=self)
                ),
                name="assets_asset_wizard",
            ),
            path(
                "schedule/capture/",
                self.admin_site.admin_view(
                    AssetScheduleCaptureView.as_view(model_admin=self)
                ),
                name="assets_asset_schedule_capture",
            ),
            path(
                "schedule/",
                self.admin_site.admin_view(
                    AssetScheduleView.as_view(model_admin=self)
                ),
                name="assets_asset_schedule",
            ),
        ]
        return custom + urls

    def add_view(self, request, form_url="", extra_context=None):
        url = reverse("admin:assets_asset_wizard")
        if request.GET:
            url = f"{url}?{request.GET.urlencode()}"
        return redirect(url)

    def qr_code_preview(self, obj):
        if obj.pk:
            url = f"/api/assets/{obj.pk}/qr/"
            return format_html(
                '<a href="{}" target="_blank">'
                '<img src="{}" style="width:200px; height:200px;" />'
                '<br><small>Haz clic para abrir en pantalla completa e imprimir</small>'
                '</a>',
                url, url
            )
        return _("Guarda el asset primero para ver el QR.")
    qr_code_preview.short_description = _("QR Code")

    def qr_link(self, obj):
        if obj.pk:
            url = f"/api/assets/{obj.pk}/qr/"
            return format_html('<a href="{}" target="_blank"> QR</a>', url)
        return "-"
    qr_link.short_description = _("QR")

    def get_identifiers(self, obj):
        return ", ".join([f"{i.label}: {i.value}" for i in obj.identifiers.all()])
    get_identifiers.short_description = _("Identifiers")

    def zone_client(self, obj):
        return obj.current_working_zone.client if obj.current_working_zone else None
    zone_client.short_description = _("Zone Client")

    @admin.action(description=_("Download QR Grid"))
    def download_qr_grid(self, request, queryset):
        buffer = generate_asset_qr_grid(queryset)
        if not buffer:
            self.message_user(request, _("No selected assets for generation."), level="error")
            return None
        response = HttpResponse(buffer.getvalue(), content_type="image/png")
        response["Content-Disposition"] = 'attachment; filename="assets_qr_grid.png"'
        return response

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.prefetch_related("identifiers")
        return qs