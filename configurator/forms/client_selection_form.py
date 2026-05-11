from django import forms
from django.conf import settings
from django.contrib import admin
from unfold.widgets import UnfoldAdminRelatedFieldWrapperWidget
from configurator.models import Configuration
from sales.models import Client, Quote
from assets.models import Asset
from unfold.fields import UnfoldAdminAutocompleteModelChoiceField
from django.utils.translation import gettext_lazy as _

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Field, Row, Column, HTML
from core.admin.company_access import get_user_companies

class ClientSelectionFormHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.form_tag = False
        self.disable_csrf = True
        self.template_pack = "unfold_crispy"

        self.layout = Layout(
            HTML(
                f"""
                <div class="col-span-12 mb-6 rounded-lg border border-gray-200 bg-white p-4">
                    <h3 class="text-base font-semibold text-gray-800 mb-3">
                        {_("Quote and Asset Information")}
                    </h3>
                """
            ),
            Row(
                Column(Field("quote"), css_class="col-span-12 md:col-span-6"),
                Column(Field("asset"), css_class="col-span-12 md:col-span-6"),
                css_class="gap-4 mb-4"
            ),
            HTML("</div>")
        )

class ClientSelectionForm(forms.ModelForm):
    quote = UnfoldAdminAutocompleteModelChoiceField(
        url_path="admin:quote_autocomplete",
        queryset=Quote.objects.all(),
        label=_("Select Quote")
    )
    asset = UnfoldAdminAutocompleteModelChoiceField(
        url_path="admin:asset_autocomplete",
        queryset=Asset.objects.all(),
        label=_("Select Asset (Optional)"),
        required=False
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

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        self.product = kwargs.pop("product", None)
        
        super().__init__(*args, **kwargs)
        
        # Optimization for GET requests: return empty results initially for autocomplete
        if self.request and self.request.method == "GET":
             if "initial" not in kwargs:
                 self.fields["quote"].queryset = Quote.objects.none()
                 self.fields["asset"].queryset = Asset.objects.none()

        # Ensure we have the right helper attached for Crispy rendering
        self.helper = ClientSelectionFormHelper()
        
        if self.request and self.request.user and not self.request.user.is_superuser:
            user_companies = get_user_companies(self.request.user)
            self.fields["quote"].queryset = self.fields["quote"].queryset.filter(company__in=user_companies)
            self.fields["asset"].queryset = self.fields["asset"].queryset.filter(company__in=user_companies)
            
        if self.product and self.product.company:
            self.fields["asset"].queryset = self.fields["asset"].queryset.filter(company=self.product.company)
            
        if self.request and self.fields.get('quote'):
            self.fields['quote'].widget = UnfoldAdminRelatedFieldWrapperWidget(
                self.fields['quote'].widget,
                self.instance._meta.get_field('quote').remote_field,
                admin.site,
                can_add_related=True
            )