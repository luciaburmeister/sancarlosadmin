from django import forms
from django.conf import settings
from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Field, Row, Column, HTML
from django.forms import CheckboxInput
from unfold.widgets import UnfoldAdminRelatedFieldWrapperWidget
from unfold.fields import UnfoldAdminAutocompleteModelChoiceField
from unfold.widgets import (
    UnfoldAdminDecimalFieldWidget,
    UnfoldAdminTextInputWidget,
    UnfoldAdminSelectWidget,
)
from sales.models import Quote
from assets.models import Asset
from configurator.models import Configuration, ParameterType
from core.admin.company_access import get_user_companies


class PresetClientLinkFormHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.form_tag = False
        self.disable_csrf = True
        self.template_pack = "unfold_crispy"

        self.layout = Layout(
            HTML(
                f"""<div class="col-span-12 mb-6 rounded-lg border border-gray-200 bg-white p-4">
                    <h3 class="text-base font-semibold text-gray-800 mb-3">
                        {_("Quote and Asset Information")}
                    </h3>"""
            ),
            Row(
                Column(Field("quote"), css_class="col-span-12 md:col-span-6"),
                Column(Field("asset"), css_class="col-span-12 md:col-span-6"),
                css_class="gap-4 mb-4",
            ),
            HTML("</div>"),
        )


class PresetClientLinkForm(forms.ModelForm):
    """Step 1 of the use-preset wizard: link client / quote."""

    quote = UnfoldAdminAutocompleteModelChoiceField(
        url_path="admin:quote_autocomplete",
        queryset=Quote.objects.all(),
        label=_("Select Quote"),
    )
    asset = UnfoldAdminAutocompleteModelChoiceField(
        url_path="admin:asset_autocomplete",
        queryset=Asset.objects.all(),
        label=_("Select Asset (Optional)"),
        required=False,
    )

    class Meta:
        model = Configuration
        fields = ["quote", "asset"]

    class Media:
        extra = "" if settings.DEBUG else ".min"
        js = (
            f"admin/js/vendor/jquery/jquery{extra}.js",
            "admin/js/vendor/select2/select2.full.js",
            "admin/js/jquery.init.js",
            "unfold/js/select2.init.js",
            "admin/js/admin/RelatedObjectLookups.js",
        )
        css = {
            "screen": (
                "admin/css/vendor/select2/select2.css",
                "admin/css/autocomplete.css",
            ),
        }

    def __init__(self, *args, preset=None, request=None, **kwargs):
        # Pop extra kwargs before super().__init__
        kwargs.pop("product", None)
        self.preset = preset
        self.request = request
        super().__init__(*args, **kwargs)

        if self.request and self.request.method == "GET":
            if "initial" not in kwargs:
                self.fields["quote"].queryset = Quote.objects.none()
                self.fields["asset"].queryset = Asset.objects.none()

        self.helper = PresetClientLinkFormHelper()

        if self.request and self.request.user and not self.request.user.is_superuser:
            user_companies = get_user_companies(self.request.user)
            self.fields["quote"].queryset = self.fields["quote"].queryset.filter(
                company__in=user_companies
            )
            self.fields["asset"].queryset = self.fields["asset"].queryset.filter(
                company__in=user_companies
            )

        if self.preset and self.preset.product and self.preset.product.company:
            self.fields["asset"].queryset = self.fields["asset"].queryset.filter(
                company=self.preset.product.company
            )

        if self.request and self.fields.get("quote"):
            self.fields["quote"].widget = UnfoldAdminRelatedFieldWrapperWidget(
                self.fields["quote"].widget,
                self.instance._meta.get_field("quote").remote_field,
                admin.site,
                can_add_related=True,
            )


class PresetMissingParamsForm(forms.Form):
    """
    Step 2 (conditional) of the use-preset wizard.
    Only shows parameters NOT covered by the preset.
    """

    def __init__(self, *args, preset=None, request=None, **kwargs):
        kwargs.pop("product", None)
        self.preset = preset
        super().__init__(*args, **kwargs)

        if not self.preset:
            return

        missing_params = self.preset.get_missing_parameters()

        for param in missing_params:
            field_name = f"param_{param.id}"

            if param.type == ParameterType.NUMBER:
                field = forms.FloatField(
                    label=param.label,
                    required=param.required,
                    initial=param.default_value,
                    widget=UnfoldAdminDecimalFieldWidget(),
                )
            elif param.type == ParameterType.TEXT:
                field = forms.CharField(
                    label=param.label,
                    required=param.required,
                    initial=param.default_value,
                    max_length=200,
                    widget=UnfoldAdminTextInputWidget(),
                )
            elif param.type == ParameterType.BOOLEAN:
                val = param.default_value
                if isinstance(val, str):
                    val = val.lower() == "true"
                else:
                    val = bool(val) if val else False

                field = forms.BooleanField(
                    label=param.label,
                    required=False,
                    initial=val,
                )
            elif param.type == ParameterType.SELECT:
                if isinstance(param.options, list):
                    choices = [(o, o) for o in param.options]
                else:
                    choices = (
                        list(param.options.items()) if param.options else []
                    )

                field = forms.ChoiceField(
                    label=param.label,
                    required=param.required,
                    initial=param.default_value,
                    choices=choices,
                    widget=UnfoldAdminSelectWidget(),
                )
            else:
                continue

            field.parameter_group = param.parameter_group or _("General")
            field.parameter_name = param.name
            self.fields[field_name] = field


class PresetMissingParamsFormHelper(FormHelper):
    def __init__(self, form, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.form = form
        self.form_tag = False
        self.disable_csrf = True
        self.template_pack = "unfold_crispy"
        self.layout = Layout(*self._build_grouped_rows())

    def _build_grouped_rows(self):
        rows = []
        grouped_fields = {}

        for name, field in self.form.fields.items():
            group = getattr(field, "parameter_group", None) or _("General")
            grouped_fields.setdefault(group, []).append(name)

        if not grouped_fields:
            return rows

        for group, field_names in grouped_fields.items():
            rows.append(
                HTML(
                    f"""<div class="col-span-12 mb-6 rounded-lg border border-gray-200 bg-white p-4">
                        <h3 class="text-base font-semibold text-gray-800 mb-3">{group}</h3>"""
                )
            )

            buffer = []
            for name in field_names:
                widget = self.form.fields[name].widget
                if isinstance(widget, CheckboxInput):
                    rows.append(
                        Row(
                            Column(Field(name), css_class="col-span-12"),
                            css_class="mb-3",
                        )
                    )
                    continue

                buffer.append(name)
                if len(buffer) == 2:
                    rows.append(
                        Row(
                            *[
                                Column(
                                    Field(n), css_class="col-span-12 md:col-span-6"
                                )
                                for n in buffer
                            ],
                            css_class="gap-4 mb-4",
                        )
                    )
                    buffer = []

            if buffer:
                rows.append(
                    Row(
                        Column(Field(buffer[0]), css_class="col-span-12"),
                        css_class="mb-4",
                    )
                )

            rows.append(HTML("</div>"))

        return rows
