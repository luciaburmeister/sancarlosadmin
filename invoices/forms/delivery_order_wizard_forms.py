from django import forms
from django.utils.translation import gettext_lazy as _
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Row, Column, Field
from decimal import Decimal

from unfold.widgets import (
    UnfoldAdminTextInputWidget,
    UnfoldAdminDecimalFieldWidget,
)


# ponytail: one form class with a crispy layout that renders inside
# stacked_inline_formset.html — same pattern as WizardItemForm

class DeliveryOrderItemForm(forms.Form):
    """One row in the delivery order items formset."""
    source_content_type_id = forms.IntegerField(widget=forms.HiddenInput, required=False)
    source_object_id = forms.IntegerField(widget=forms.HiddenInput, required=False)
    description = forms.CharField(
        max_length=500,
        widget=UnfoldAdminTextInputWidget,
        label=_("Description"),
    )
    quantity = forms.DecimalField(
        max_digits=10,
        decimal_places=3,
        widget=UnfoldAdminDecimalFieldWidget,
        label=_("Quantity"),
    )
    unit_price = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        widget=UnfoldAdminDecimalFieldWidget,
        label=_("Unit Price"),
    )
    discount_percentage = forms.DecimalField(
        max_digits=5,
        decimal_places=2,
        initial=Decimal("0.00"),
        widget=UnfoldAdminDecimalFieldWidget,
        label=_("Discount (%)"),
        required=False,
    )
    reference = forms.CharField(
        max_length=200,
        required=False,
        widget=UnfoldAdminTextInputWidget,
        label=_("Reference"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Field("source_content_type_id"),
            Field("source_object_id"),
            Row(
                Column(Field("description"), css_class="col-span-12 md:col-span-6"),
                Column(Field("reference"), css_class="col-span-12 md:col-span-6"),
                css_class="gap-4 mb-4"
            ),
            Row(
                Column(Field("quantity"), css_class="col-span-12 md:col-span-4"),
                Column(Field("unit_price"), css_class="col-span-12 md:col-span-4"),
                Column(Field("discount_percentage"), css_class="col-span-12 md:col-span-4"),
                css_class="gap-4"
            ),
        )
