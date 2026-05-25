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
    WizardItemFormsetHelper,
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
        and direct configurations, scaled by the selected percentage/amount.
        Each configuration becomes a separate line item on the invoice.
        Items use subtotal (pre-tax). Each item carries a unique source_id
        and its per-config tax amount for reliable lookup.
        """
        setup_data = self.get_cleaned_data_for_step("setup")
        if not setup_data:
            return []

        mode = setup_data.get('invoice_mode', 'percentage')
        percentage = setup_data.get('percentage', Decimal("100"))
        calculated_subtotal = setup_data.get('_calculated_subtotal', Decimal("0.00"))

        items_initial = []
        summary = self.invoicing_summary

        total_sub = summary['total_invoiceable_subtotal']
        if total_sub > 0:
            ratio = calculated_subtotal / total_sub
        else:
            ratio = Decimal("0")

        for quote_data in summary['quotes']:
            quote = quote_data['quote']

            for config in quote.configurations.all():
                sub = config.subtotal_price.amount
                tot = config.total_price.amount
                item_price = (sub * ratio).quantize(Decimal("0.01"))
                item_tax = ((tot - sub) * ratio).quantize(Decimal("0.01"))
                if item_price <= 0:
                    continue
                items_initial.append({
                    'source_id': f"config_{config.id}",
                    'description': f"{config.name} (Q#{quote.id})",
                    'quantity': Decimal("1"),
                    'unit_price': item_price,
                    'discount_percentage': Decimal("0.00"),
                    '_tax_amount': item_tax,
                })

            for item in quote.items.all():
                item_price = (item.line_total.amount * ratio).quantize(Decimal("0.01"))
                if item_price <= 0:
                    continue
                items_initial.append({
                    'source_id': f"quote_item_{item.id}",
                    'description': f"{item.material} x{item.quantity} (Q#{quote.id})",
                    'quantity': Decimal("1"),
                    'unit_price': item_price,
                    'discount_percentage': Decimal("0.00"),
                    '_tax_amount': Decimal("0.00"),
                })

        for config_data in summary.get('direct_configurations', []):
            sub = config_data['subtotal']
            tot = config_data['total']
            item_price = (sub * ratio).quantize(Decimal("0.01"))
            item_tax = ((tot - sub) * ratio).quantize(Decimal("0.01"))
            if item_price <= 0:
                continue
            items_initial.append({
                'source_id': f"direct_{config_data['id']}",
                'description': config_data['name'],
                'quantity': Decimal("1"),
                'unit_price': item_price,
                'discount_percentage': Decimal("0.00"),
                '_tax_amount': item_tax,
            })

        return items_initial

    def get_form_initial(self, step):
        initial = super().get_form_initial(step)

        if step == "items":
            return self._build_items_initial()

        return initial

    def get_form(self, step=None, data=None, files=None):
        form = super().get_form(step, data, files)

        if step == "items" and not data:
            items_initial = self._build_items_initial()
            if items_initial:
                FormSet = formset_factory(WizardItemForm, extra=0, can_delete=True)
                form = FormSet(
                    data=data,
                    files=files,
                    prefix=self.get_form_prefix(step, FormSet),
                    initial=items_initial,
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
        elif self.steps.current == "notes":
            context["helper"] = context["form"].helper

        return context

    def done(self, form_list, **kwargs):
        all_forms = list(form_list)
        setup_form = all_forms[0]
        items_formset = all_forms[1]
        notes_form = all_forms[2]

        # Build tax lookup keyed by source_id (unique per config/item)
        items_initial = self._build_items_initial()
        tax_lookup = {
            item['source_id']: item.get('_tax_amount', Decimal("0.00"))
            for item in items_initial
        }

        items_data = []
        total_tax = Decimal("0.00")
        for form in items_formset:
            if not form.cleaned_data or form.cleaned_data.get('DELETE'):
                continue
            source_id = form.cleaned_data.get('source_id', '')
            item_tax = tax_lookup.get(source_id, Decimal("0.00"))
            total_tax += item_tax
            items_data.append({
                'source_id': source_id,
                'description': form.cleaned_data.get('description', ''),
                'quantity': form.cleaned_data.get('quantity', Decimal("1")),
                'unit_price': form.cleaned_data.get('unit_price'),
                'discount_percentage': form.cleaned_data.get('discount_percentage', Decimal("0.00")),
            })

        try:
            invoice = ProjectFileInvoiceService.create_invoice_from_project_file(
                project_file=self.project_file,
                company=setup_form.cleaned_data['company'],
                client=self.project_file.client,
                date_issued=setup_form.cleaned_data['date_issued'],
                due_date=setup_form.cleaned_data.get('due_date'),
                items_data=items_data,
                total_tax=total_tax,
                notes=notes_form.cleaned_data.get('notes', ''),
                payment_method=setup_form.cleaned_data.get('payment_method'),
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


