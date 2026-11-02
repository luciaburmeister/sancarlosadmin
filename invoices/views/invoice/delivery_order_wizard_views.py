from decimal import Decimal
from django.shortcuts import redirect, get_object_or_404
from django.urls import reverse
from django.contrib import messages
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.forms import formset_factory
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from management.models import ProjectFile
from invoices.forms.delivery_order_wizard_forms import DeliveryOrderItemForm
from invoices.services.delivery_order_service import DeliveryOrderService


# ponytail: TemplateView so get_context_data works with UnfoldModelAdminViewMixin
class DeliveryOrderWizardView(UnfoldModelAdminViewMixin, TemplateView):
    template_name = "invoices/delivery_order_wizard.html"
    permission_required = ("sales.add_deliveryorder",)
    title = _("Create Delivery Order")

    ItemFormSet = formset_factory(DeliveryOrderItemForm, extra=0, can_delete=True)

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.project_file = get_object_or_404(
            ProjectFile, pk=kwargs.get("project_file_id")
        )
        self.delivery_summary = DeliveryOrderService.get_delivery_summary(
            self.project_file
        )

    def _items_initial(self):
        return DeliveryOrderService.proposed_items(self.delivery_summary.get("deliverable_items", []))

    def get_context_data(self, **kwargs):
        # ponytail: super() chains through UnfoldModelAdminViewMixin which injects
        # each_context, title, model_admin — that's what makes Unfold render properly
        context = super().get_context_data(**kwargs)
        context["project_file"] = self.project_file
        context["delivery_summary"] = self.delivery_summary
        return context

    def get(self, request, *args, **kwargs):
        formset = self.ItemFormSet(initial=self._items_initial())
        context = self.get_context_data(formset=formset, notes="")
        return self.render_to_response(context)

    def post(self, request, *args, **kwargs):
        formset = self.ItemFormSet(request.POST)
        notes = request.POST.get("notes", "")

        if not formset.is_valid():
            context = self.get_context_data(formset=formset, notes=notes)
            return self.render_to_response(context)

        items_data = [
            {
                "source_content_type_id": f.cleaned_data.get("source_content_type_id"),
                "source_object_id": f.cleaned_data.get("source_object_id"),
                "description": f.cleaned_data.get("description", ""),
                "quantity": f.cleaned_data.get("quantity", Decimal("1")),
                "unit_price": f.cleaned_data.get("unit_price", Decimal("0")),
                "discount_percentage": f.cleaned_data.get("discount_percentage", Decimal("0.00")),
                "reference": f.cleaned_data.get("reference", ""),
            }
            for f in formset
            if f.cleaned_data and not f.cleaned_data.get("DELETE")
        ]

        try:
            # ponytail: company from project_file, date=today — no setup form needed
            do = DeliveryOrderService.create_delivery_order(
                project_file=self.project_file,
                company=self.project_file.company,
                client=self.project_file.client,
                items_data=items_data,
                notes=notes,
            )
            messages.success(
                request,
                _("Delivery Order %(number)s created.") % {"number": do.number},
            )
            return redirect(
                reverse("admin:sales_deliveryorder_change", args=[do.id])
            )
        except ValidationError as e:
            messages.error(request, str(e.message))
            return redirect(
                reverse(
                    "admin:management_projectfile_change",
                    args=[self.project_file.id],
                )
            )
