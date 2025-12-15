from django import forms
from django.forms import BaseFormSet
from materials.models import Component
from unfold.widgets import (
    UnfoldAdminSelect2Widget,
    UnfoldAdminIntegerFieldWidget,
)
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Field, Row, Column

class AddonFormsetHelper(FormHelper):
    """
    Crispy helper for the AddonForm formset, matching Unfold docs.
    Enables dynamic add/remove rows WITHOUT custom JS.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.template = "unfold_crispy/layout/table_inline_formset.html"
        self.form_id = "addon-formset"      
        self.form_add = True                
        self.form_tag = False           # Wizard handles <form>
        self.template_pack = "unfold_crispy"

class AddonForm(forms.Form):
    addons = forms.ModelChoiceField(
        queryset=Component.objects.all(),
        widget=UnfoldAdminSelect2Widget,
        required=False,
        label="Select Add-ons",
    )

    quantity = forms.DecimalField(
        min_value=0.01,
        initial=1,
        decimal_places=2,
        label="Quantity",
        widget=UnfoldAdminIntegerFieldWidget,
    )

    def __init__(self, *args, product=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.product = product

        self.helper = FormHelper()
        self.helper.form_tag = False 
        self.helper.layout = Layout(
            Row(
                Column(Field("addons"), css_class="col-8"),
                Column(Field("quantity"), css_class="col-4"),
            )
        )


class BaseAddonFormSet(BaseFormSet):
    def __init__(self, *args, product=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.product = product

    def _construct_form(self, i, **kwargs):
        kwargs["product"] = self.product
        return super()._construct_form(i, **kwargs)
