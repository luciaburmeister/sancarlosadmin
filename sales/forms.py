from django import forms
from django.utils.translation import gettext_lazy as _

from import_export.forms import ConfirmImportForm as BaseConfirmImportForm
from unfold.contrib.import_export.forms import ImportForm
from unfold.contrib.import_export.forms import ExportForm as UnfoldExportForm
from unfold.widgets import UnfoldAdminSelect2Widget, UnfoldAdminRelatedFieldWrapperWidget

from company.models import Company


# ─────────────────────────────────────────────────────────────────────────────
# Helper — wraps a field's widget with the Unfold FK button bar
# ─────────────────────────────────────────────────────────────────────────────

def _wrap_fk(field, remote_field, user, admin_site=None):
    from django.contrib.admin import site as default_admin_site
    if admin_site is None:
        admin_site = default_admin_site

    opts = remote_field.model._meta
    field.widget = UnfoldAdminRelatedFieldWrapperWidget(
        widget=field.widget,
        rel=remote_field,
        admin_site=admin_site,
        can_add_related=user.has_perm(f"{opts.app_label}.add_{opts.model_name}"),
        can_change_related=user.has_perm(f"{opts.app_label}.change_{opts.model_name}"),
        can_delete_related=user.has_perm(f"{opts.app_label}.delete_{opts.model_name}"),
        can_view_related=user.has_perm(f"{opts.app_label}.view_{opts.model_name}"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Mixin — applies Select2 to the format field (export forms only).
# ─────────────────────────────────────────────────────────────────────────────

class _Select2FormatMixin:
    def _apply_select2_format(self):
        self.fields["format"].choices = [
            (k, v) for k, v in self.fields["format"].choices if k != ""
        ]
        self.fields["format"].widget = UnfoldAdminSelect2Widget(
            choices=self.fields["format"].choices,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Mixin — hidden format field with magic-byte-based auto-detection.
#
# Inspects the first 4 bytes of the uploaded file instead of relying on the
# file extension, so that .xls files that are actually XLSX internally (common
# when saved by modern Excel) are correctly identified and not passed to xlrd.
#   PK\x03\x04  → ZIP container → XLSX
#   \xd0\xcf\x11\xe0 → BIFF Compound Document → XLS
# Falls back to the file extension if neither magic signature matches.
# ─────────────────────────────────────────────────────────────────────────────

class _AutoFormatMixin:
    def clean_format(self):
        import_file = self.cleaned_data.get("import_file")
        if not import_file:
            return "0"

        # Read magic bytes to determine the real file type
        import_file.seek(0)
        magic = import_file.read(4)
        import_file.seek(0)

        if magic[:2] == b'PK':
            target_ext = "xlsx"
        elif magic[:4] == b'\xd0\xcf\x11\xe0':
            target_ext = "xls"
        elif "." in import_file.name:
            target_ext = import_file.name.rsplit(".", 1)[-1].lower()
        else:
            return "0"

        for i, fmt_class in enumerate(self._formats):
            try:
                if fmt_class().get_title().lower() == target_ext:
                    return str(i)
            except Exception:
                continue

        return "0"  # default to CSV


# ─────────────────────────────────────────────────────────────────────────────
# Client Import Form
# ─────────────────────────────────────────────────────────────────────────────

class ClientImportForm(_AutoFormatMixin, ImportForm):
    """
    Import form with:
    - Hidden format field (auto-detected from file magic bytes).
    - Company selector with Select2, Unfold FK buttons, and a × clear button.
    """

    company = forms.ModelChoiceField(
        queryset=Company.objects.none(),  # restricted in __init__
        label=_("Company"),
        required=True,
        empty_label="",
        widget=UnfoldAdminSelect2Widget(attrs={
            "data-allow-clear": "true",
            "data-placeholder": "",
        }),
    )

    field_order = ["resource", "import_file", "company"]

    def __init__(self, formats, resources, companies=None, request=None, **kwargs):
        super().__init__(formats, resources, **kwargs)
        self._formats = formats

        # Restrict company choices to what the requesting user can access
        if companies is not None:
            self.fields["company"].queryset = companies
            if companies.count() == 1:
                self.fields["company"].initial = companies.first()

        # Hide the format field — auto-detected from file magic bytes
        self.fields["format"].widget = forms.HiddenInput()
        self.fields["format"].required = False

        # Add Unfold FK button bar (edit / add / view) to the company field
        if request is not None:
            from sales.models import Client
            company_remote_field = Client._meta.get_field("company").remote_field
            _wrap_fk(self.fields["company"], company_remote_field, request.user)


# ─────────────────────────────────────────────────────────────────────────────
# Client Confirm Import Form — carries company to the second POST step
# ─────────────────────────────────────────────────────────────────────────────

class ClientConfirmImportForm(BaseConfirmImportForm):
    """Extends the standard confirm form with a hidden company field."""
    company = forms.CharField(widget=forms.HiddenInput(), required=False)


# ─────────────────────────────────────────────────────────────────────────────
# Client / Quote Export Form
# ─────────────────────────────────────────────────────────────────────────────

class ClientExportForm(_Select2FormatMixin, UnfoldExportForm):
    """
    Export form with Select2 on format and no "---" blank option.
    Drop-in replacement for unfold.contrib.import_export.forms.ExportForm.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._apply_select2_format()


# ─────────────────────────────────────────────────────────────────────────────
# Quote Confirm Import Form — carries company to the second POST step
# ─────────────────────────────────────────────────────────────────────────────

class QuoteConfirmImportForm(BaseConfirmImportForm):
    """Extends the standard confirm form with a hidden company field."""
    company = forms.CharField(widget=forms.HiddenInput(), required=False)


# ─────────────────────────────────────────────────────────────────────────────
# Quote Import Form
# ─────────────────────────────────────────────────────────────────────────────

class ConfigurationItemConfirmImportForm(BaseConfirmImportForm):
    """Carries company through the confirm step for ConfigurationItem import."""
    company = forms.CharField(widget=forms.HiddenInput(), required=False)


class ConfigurationItemImportForm(_AutoFormatMixin, ImportForm):
    """
    Import form for ConfigurationItems (tailored/configured items per quote).
    Columns: Número de documento | Código de configuración | Código de artículo
             | Descripción | Cantidad | Precio unitario
    """

    company = forms.ModelChoiceField(
        queryset=Company.objects.none(),
        label=_("Company"),
        required=True,
        empty_label="",
        widget=UnfoldAdminSelect2Widget(attrs={
            "data-allow-clear": "true",
            "data-placeholder": "",
        }),
    )

    field_order = ["resource", "import_file", "company"]

    def __init__(self, formats, resources, companies=None, request=None, **kwargs):
        super().__init__(formats, resources, **kwargs)
        self._formats = formats

        if companies is not None:
            self.fields["company"].queryset = companies
            if companies.count() == 1:
                self.fields["company"].initial = companies.first()

        self.fields["format"].widget = forms.HiddenInput()
        self.fields["format"].required = False

        if request is not None:
            from configurator.models import Configuration
            company_remote_field = Configuration._meta.get_field("company").remote_field
            _wrap_fk(self.fields["company"], company_remote_field, request.user)


class QuoteItemConfirmImportForm(BaseConfirmImportForm):
    """Extends the standard confirm form with a hidden company field."""
    company = forms.CharField(widget=forms.HiddenInput(), required=False)


class QuoteItemImportForm(_AutoFormatMixin, ImportForm):
    """
    Import form for QuoteItems.
    Columns: Número de documento | Referencia | Descripción | Cantidad | Precio unitario
    Format field is hidden and auto-detected from file magic bytes.
    Company selector scopes quote and material lookups.
    """

    company = forms.ModelChoiceField(
        queryset=Company.objects.none(),
        label=_("Company"),
        required=True,
        empty_label="",
        widget=UnfoldAdminSelect2Widget(attrs={
            "data-allow-clear": "true",
            "data-placeholder": "",
        }),
    )

    field_order = ["resource", "import_file", "company"]

    def __init__(self, formats, resources, companies=None, request=None, **kwargs):
        super().__init__(formats, resources, **kwargs)
        self._formats = formats

        if companies is not None:
            self.fields["company"].queryset = companies
            if companies.count() == 1:
                self.fields["company"].initial = companies.first()

        self.fields["format"].widget = forms.HiddenInput()
        self.fields["format"].required = False

        if request is not None:
            from sales.models import Quote
            company_remote_field = Quote._meta.get_field("company").remote_field
            _wrap_fk(self.fields["company"], company_remote_field, request.user)


class QuoteImportForm(_AutoFormatMixin, ImportForm):
    """
    Import form for Quotes.
    Columns in file: Número de documento | Fecha | Nombre de cliente | Total | Estado
    Format field is hidden and auto-detected from file magic bytes.
    Company selector restricts client lookup and sets quote.company.
    """

    company = forms.ModelChoiceField(
        queryset=Company.objects.none(),  # restricted in __init__
        label=_("Company"),
        required=True,
        empty_label="",
        widget=UnfoldAdminSelect2Widget(attrs={
            "data-allow-clear": "true",
            "data-placeholder": "",
        }),
    )

    field_order = ["resource", "import_file", "company"]

    def __init__(self, formats, resources, companies=None, request=None, **kwargs):
        super().__init__(formats, resources, **kwargs)
        self._formats = formats

        if companies is not None:
            self.fields["company"].queryset = companies
            if companies.count() == 1:
                self.fields["company"].initial = companies.first()

        # Hide the format field — auto-detected from file magic bytes
        self.fields["format"].widget = forms.HiddenInput()
        self.fields["format"].required = False

        if request is not None:
            from sales.models import Quote
            company_remote_field = Quote._meta.get_field("company").remote_field
            _wrap_fk(self.fields["company"], company_remote_field, request.user)
