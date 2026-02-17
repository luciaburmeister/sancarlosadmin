from django import forms
from django.conf import settings
from django.contrib import admin
from unfold.widgets import UnfoldRelatedFieldWidgetWrapper
from configurator.models import Configuration
from sales.models import Client
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
                        {_("Client and Asset Information")}
                    </h3>
                """
            ),
            Row(
                Column(Field("client"), css_class="col-span-12 md:col-span-6"),
                Column(Field("asset"), css_class="col-span-12 md:col-span-6"),
                css_class="gap-4 mb-4"
            ),
            HTML("</div>")
        )

class ClientSelectionForm(forms.ModelForm):
    client = UnfoldAdminAutocompleteModelChoiceField(
        url_path="admin:client_autocomplete",
        queryset=Client.objects.all(),
        label=_("Select Client")
    )
    asset = UnfoldAdminAutocompleteModelChoiceField(
        url_path="admin:asset_autocomplete",
        queryset=Asset.objects.all(),
        label=_("Select Asset (Optional)"),
        required=False
    )

    class Meta:
        model = Configuration
        fields = ["client", "asset"]

    class Media:
        # Manually include Select2 and Unfold init media
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
                 self.fields["client"].queryset = Client.objects.none()
                 self.fields["asset"].queryset = Asset.objects.none()

        # Ensure we have the right helper attached for Crispy rendering
        self.helper = ClientSelectionFormHelper()
        
        # Restrict by user company if applicable.
        if self.request and self.request.user and not self.request.user.is_superuser:
            user_companies = get_user_companies(self.request.user)
            self.fields["client"].queryset = self.fields["client"].queryset.filter(company__in=user_companies)
            self.fields["asset"].queryset = self.fields["asset"].queryset.filter(company__in=user_companies)
            
        # Further filter assets if a product is provided
        if self.product and self.product.company:
            self.fields["asset"].queryset = self.fields["asset"].queryset.filter(company=self.product.company)
            
        # Add "Add Client" button using RelatedFieldWidgetWrapper
        if self.request and self.fields.get('client'):
             # We need an admin site instance. Using standard admin.site is usually fine.
            self.fields['client'].widget = UnfoldRelatedFieldWidgetWrapper(
                self.fields['client'].widget,
                self.instance._meta.get_field('client').remote_field,
                admin.site,
                can_add_related=True
            )
