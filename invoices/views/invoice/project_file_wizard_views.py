from decimal import Decimal
from django.shortcuts import redirect, get_object_or_404
from django.urls import reverse
from django.contrib import messages
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.forms import formset_factory

from unfold.views import UnfoldModelAdminViewMixin
from formtools.wizard.views import SessionWizardView

from management.models import ProjectFile
from invoices.models import SalesInvoice, SalesInvoiceItem, AppliedTax
from django.contrib.contenttypes.models import ContentType
from invoices.forms.wizard_forms import (
    WizardItemForm,
    WizardTaxForm,
    WizardItemFormsetHelper,
    WizardTaxFormsetHelper,
)
from invoices.forms.project_file_wizard_forms import (
    ProjectFileInvoiceSetupForm,
    ProjectFileInvoiceNotesForm,
)
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService


class ProjectFileInvoiceWizardView(UnfoldModelAdminViewMixin, SessionWizardView):
    form_list = [
        ("setup", ProjectFileInvoiceSetupForm),
        ("items", formset_factory(WizardItemForm, extra=1, can_delete=True)),
        ("taxes", formset_factory(WizardTaxForm, extra=0, can_delete=True)),
        ("notes", ProjectFileInvoiceNotesForm),
    ]
    template_name = "invoices/project_file_invoice_wizard.html"
    permission_required = ("invoices.add_salesinvoice",)
    title = _("Create Invoice from Project File")

    def dispatch(self, request, *args, **kwargs):
        self.project_file = get_object_or_404(ProjectFile, pk=kwargs.get('project_file_id'))
        self.invoicing_summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self, step):
        kwargs = super().get_form_kwargs(step)
        if step == "setup":
            kwargs["request"] = self.request
            kwargs["project_file"] = self.project_file
            kwargs["invoicing_summary"] = self.invoicing_summary
        elif step == "notes":
            kwargs["request"] = self.request
        return kwargs

    def _build_items_initial(self):
        """
        Builds the initial items list from the accepted quotes' configurations
        and loose items, scaled by the selected percentage/amount.
        Each configuration becomes a separate line item on the invoice.
        """
        setup_data = self.get_cleaned_data_for_step("setup")
        if not setup_data:
            return []

        mode = setup_data.get('invoice_mode', 'percentage')
        percentage = setup_data.get('percentage', Decimal("100"))
        calculated_subtotal = setup_data.get('_calculated_subtotal', Decimal("0.00"))

        # Get all configurations + loose items from accepted quotes
        items_initial = []
        summary = self.invoicing_summary

        if mode == 'percentage':
            ratio = percentage / Decimal("100")
        else:
            # Custom amount: calculate the effective ratio from remaining subtotal
            remaining = summary['remaining_subtotal']
            ratio = (calculated_subtotal / remaining) if remaining > 0 else Decimal("1")

        for quote_data in summary['quotes']:
            quote = quote_data['quote']

            # Add configurations as individual items
            for config in quote.configurations.all():
                item_price = (config.subtotal_price.amount * ratio).quantize(Decimal("0.01"))
                if item_price <= 0:
                    continue
                items_initial.append({
                    'description': f"{config.name} (Q#{quote.id})",
                    'quantity': Decimal("1"),
                    'unit_price': item_price,
                    'discount_percentage': Decimal("0.00"),
                })

            # Add loose quote items (QuoteItems not tied to configurations)
            for item in quote.items.all():
                item_price = (item.line_total.amount * ratio).quantize(Decimal("0.01"))
                if item_price <= 0:
                    continue
                items_initial.append({
                    'description': f"{item.material} x{item.quantity} (Q#{quote.id})",
                    'quantity': Decimal("1"),
                    'unit_price': item_price,
                    'discount_percentage': Decimal("0.00"),
                })

        return items_initial

    def get_form_initial(self, step):
        initial = super().get_form_initial(step)

        if step == "items":
            return self._build_items_initial()

        if step == "taxes":
            # Pre-populate with taxes from accepted quotes
            quote_taxes = ProjectFileInvoiceService.get_aggregated_quote_taxes(self.project_file)
            initial = [
                {
                    'tax_type': t['tax_type'],
                    'selected_percentage': t['selected_percentage'],
                }
                for t in quote_taxes
            ]
            return initial

        return initial

    def get_form(self, step=None, data=None, files=None):
        form = super().get_form(step, data, files)

        if step == "items" and not data:
            items_initial = self._build_items_initial()
            if items_initial:
                num_items = len(items_initial)
                FormSet = formset_factory(WizardItemForm, extra=num_items, can_delete=True)
                form = FormSet(
                    data=data,
                    files=files,
                    prefix=self.get_form_prefix(step, FormSet),
                    initial=items_initial,
                )

        if step == "taxes" and not data:
            quote_taxes = ProjectFileInvoiceService.get_aggregated_quote_taxes(self.project_file)
            if len(quote_taxes) > 0:
                FormSet = formset_factory(WizardTaxForm, extra=len(quote_taxes), can_delete=True)
                form = FormSet(
                    data=data,
                    files=files,
                    prefix=self.get_form_prefix(step, FormSet),
                    initial=self.get_form_initial(step),
                )

        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['project_file'] = self.project_file
        context['invoicing_summary'] = self.invoicing_summary
        context['prefix'] = self.steps.current

        if self.steps.current == "setup":
            context["helper"] = context["form"].helper
        elif self.steps.current == "items":
            helper = WizardItemFormsetHelper()
            context["helper"] = helper
            context["formset_id"] = helper.formset_id
            context["form_id"] = helper.form_id
            context["form_add"] = helper.form_add
        elif self.steps.current == "taxes":
            helper = WizardTaxFormsetHelper()
            context["helper"] = helper
            context["formset_id"] = helper.formset_id
            context["form_id"] = helper.form_id
            context["form_add"] = helper.form_add
        elif self.steps.current == "notes":
            context["helper"] = context["form"].helper

        return context

    def done(self, form_list, **kwargs):
        all_forms = list(form_list)
        setup_form = all_forms[0]
        items_formset = all_forms[1]
        taxes_formset = all_forms[2]
        notes_form = all_forms[3]

        # Collect items data
        items_data = []
        for form in items_formset:
            if not form.cleaned_data or form.cleaned_data.get('DELETE'):
                continue
            items_data.append({
                'description': form.cleaned_data.get('description'),
                'quantity': form.cleaned_data.get('quantity', Decimal("1")),
                'unit_price': form.cleaned_data.get('unit_price'),
                'discount_percentage': form.cleaned_data.get('discount_percentage', Decimal("0.00")),
            })

        # Collect taxes data
        taxes_data = []
        for form in taxes_formset:
            if not form.cleaned_data or form.cleaned_data.get('DELETE'):
                continue
            taxes_data.append({
                'tax_type': form.cleaned_data.get('tax_type'),
                'selected_percentage': form.cleaned_data.get('selected_percentage'),
            })

        try:
            invoice = ProjectFileInvoiceService.create_invoice_from_project_file(
                project_file=self.project_file,
                company=setup_form.cleaned_data['company'],
                client=self.project_file.client,
                date_issued=setup_form.cleaned_data['date_issued'],
                due_date=setup_form.cleaned_data.get('due_date'),
                items_data=items_data,
                taxes_data=taxes_data,
                notes=notes_form.cleaned_data.get('notes', ''),
            )

            messages.success(
                self.request,
                _("Sales Invoice created successfully from %(ref)s.") % {
                    'ref': self.project_file.reference
                }
            )

            return redirect(
                reverse("admin:invoices_salesinvoice_change", args=[invoice.id])
            )

        except ValidationError as e:
            messages.error(self.request, str(e.message))
            return redirect(
                reverse("admin:management_projectfile_change", args=[self.project_file.id])
            )
