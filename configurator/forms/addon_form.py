from django import forms
from materials.models import Material
from unfold.widgets import UnfoldAdminSelectWidget, UnfoldAdminIntegerFieldWidget, UnfoldAdminSelect2Widget
from django.forms import BaseFormSet

class AddonForm(forms.Form):
    addons = forms.ModelChoiceField(
        queryset=Material.objects.all(),
        widget=UnfoldAdminSelect2Widget, 
        required=False,
        label="Select Add-ons"
    )
    
    quantity = forms.DecimalField(
        min_value=0.01,
        initial=1,
        decimal_places=2,
        label="Quantity",
        widget=UnfoldAdminIntegerFieldWidget
    )

    def __init__(self, *args, product=None, **kwargs):
        self.product = product
        super().__init__(*args, **kwargs)


class BaseAddonFormSet(BaseFormSet):
    def __init__(self, *args, product=None, **kwargs):
        self.product = product
        super().__init__(*args, **kwargs)

    def _construct_form(self, i, **kwargs):
        kwargs['product'] = self.product
        return super()._construct_form(i, **kwargs)
