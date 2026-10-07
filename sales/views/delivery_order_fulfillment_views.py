"""Dispatch / confirm-delivery screens behind the delivery order admin buttons.

Both ask for what the step records (how the order left, who received it) and
hand it to ``DeliveryOrderFulfillmentService``, which owns the transition.
"""
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column, Field, Layout, Row
from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View
from unfold.widgets import (
    UnfoldAdminDateWidget,
    UnfoldAdminIntegerFieldWidget,
    UnfoldAdminSelectWidget,
    UnfoldAdminTextInputWidget,
    UnfoldAdminURLInputWidget,
)

from sales.models import DeliveryOrder, DeliveryOrderStatus, DeliveryShippingMethod, ShippingAgency
from sales.services.delivery_order_fulfillment_service import DeliveryOrderFulfillmentService


def _helper(*rows):
    helper = FormHelper()
    helper.form_tag = False
    helper.disable_csrf = True
    helper.template_pack = "unfold_crispy"
    helper.layout = Layout(*[
        Row(
            *[Column(Field(name), css_class=f"col-span-12 md:col-span-{12 // len(row)}") for name in row],
            css_class="gap-4 mb-4",
        )
        for row in rows
    ])
    return helper


class DispatchForm(forms.Form):
    dispatched_at = forms.DateField(label=_("Dispatch Date"), widget=UnfoldAdminDateWidget)
    shipping_method = forms.ChoiceField(
        label=_("Shipping Method"),
        choices=DeliveryShippingMethod.choices,
        widget=UnfoldAdminSelectWidget,
    )
    shipping_agency = forms.ModelChoiceField(
        label=_("Shipping Agency"),
        queryset=ShippingAgency.objects.none(),
        required=False,
        widget=UnfoldAdminSelectWidget,
    )
    package_count = forms.IntegerField(
        label=_("Number of Packages"), min_value=0, required=False, widget=UnfoldAdminIntegerFieldWidget,
    )
    shipment_number = forms.CharField(
        label=_("Shipment Number"), max_length=100, required=False, widget=UnfoldAdminTextInputWidget,
    )
    tracking_url = forms.URLField(
        label=_("Tracking URL"), max_length=500, required=False, widget=UnfoldAdminURLInputWidget,
    )

    def __init__(self, *args, delivery_order, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["shipping_agency"].queryset = ShippingAgency.objects.filter(
            company=delivery_order.company, is_active=True,
        )
        self.helper = _helper(
            ("dispatched_at", "shipping_method"),
            ("shipping_agency", "package_count"),
            ("shipment_number", "tracking_url"),
        )

    def clean(self):
        data = super().clean()
        if data.get("shipping_method") == DeliveryShippingMethod.CARRIER and not data.get("shipping_agency"):
            self.add_error("shipping_agency", _("Choose the carrier that takes the order."))
        return data


class ConfirmDeliveryForm(forms.Form):
    delivered_at = forms.DateField(label=_("Delivery Date"), widget=UnfoldAdminDateWidget)
    received_by = forms.CharField(
        label=_("Received By"), max_length=255, required=False, widget=UnfoldAdminTextInputWidget,
    )

    def __init__(self, *args, delivery_order, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = _helper(("delivered_at", "received_by"))


class _FulfillmentStepView(View):
    """Subclass and set the form, the target status and ``perform``."""

    model_admin = None
    form_class = None
    target_status = None
    title = None
    submit_label = None
    template_name = "admin/sales/deliveryorder/fulfillment_step.html"

    def get_initial(self, delivery_order):
        return {}

    def perform(self, delivery_order, data):
        raise NotImplementedError

    def dispatch(self, request, *args, **kwargs):
        if not request.user.has_perm("sales.change_deliveryorder"):
            raise PermissionDenied
        self.delivery_order = get_object_or_404(DeliveryOrder, pk=kwargs["delivery_order_id"])
        if not DeliveryOrderFulfillmentService.can_transition(self.delivery_order, self.target_status):
            self.model_admin.message_user(
                request, _("This delivery order cannot take that step in its current status."), level="error",
            )
            return redirect(self._change_url())
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, delivery_order_id):
        form = self.form_class(initial=self.get_initial(self.delivery_order), delivery_order=self.delivery_order)
        return self._render(request, form)

    def post(self, request, delivery_order_id):
        form = self.form_class(request.POST, delivery_order=self.delivery_order)
        if form.is_valid():
            try:
                self.perform(self.delivery_order, form.cleaned_data)
            except ValidationError as e:
                form.add_error(None, e)
            else:
                self.model_admin.message_user(request, self.success_message % {"number": self.delivery_order.number})
                return redirect(self._change_url())
        return self._render(request, form)

    def _change_url(self):
        return reverse("admin:sales_deliveryorder_change", args=[self.delivery_order.pk])

    def _render(self, request, form):
        context = {
            **self.model_admin.admin_site.each_context(request),
            "form": form,
            "delivery_order": self.delivery_order,
            "cancel_url": self._change_url(),
            "title": self.title,
            "submit_label": self.submit_label,
            "opts": DeliveryOrder._meta,
        }
        return TemplateResponse(request, self.template_name, context)


class DeliveryOrderDispatchView(_FulfillmentStepView):
    form_class = DispatchForm
    target_status = DeliveryOrderStatus.DISPATCHED
    title = _("Dispatch Delivery Order")
    submit_label = _("Dispatch")
    success_message = _("Delivery Order %(number)s dispatched; its stock is now out of the warehouse.")

    def get_initial(self, delivery_order):
        return {
            "dispatched_at": timezone.localdate(),
            "shipping_method": delivery_order.shipping_method or DeliveryShippingMethod.CARRIER,
            "shipping_agency": delivery_order.shipping_agency_id,
            "package_count": delivery_order.package_count,
            "shipment_number": delivery_order.shipment_number,
            "tracking_url": delivery_order.tracking_url,
        }

    def perform(self, delivery_order, data):
        DeliveryOrderFulfillmentService.dispatch(delivery_order, **data)


class DeliveryOrderConfirmDeliveryView(_FulfillmentStepView):
    form_class = ConfirmDeliveryForm
    target_status = DeliveryOrderStatus.DELIVERED
    title = _("Confirm Delivery")
    submit_label = _("Confirm Delivery")
    success_message = _("Delivery Order %(number)s marked as delivered.")

    def get_initial(self, delivery_order):
        return {"delivered_at": timezone.localdate()}

    def perform(self, delivery_order, data):
        DeliveryOrderFulfillmentService.confirm_delivery(delivery_order, **data)
