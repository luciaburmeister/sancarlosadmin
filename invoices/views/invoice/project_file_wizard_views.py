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
        Builds the initial items list from the accepted quotes' configurations,
        direct configurations and work entries, scaled by the selected
        percentage/amount. Each source becomes a separate line item on the invoice.
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

        if ratio <= 0:
            return []

        for quote_data in summary['quotes']:
            quote = quote_data['quote']

            for config in quote.configurations.all():
                sub = config.subtotal_price.amount
                tot = config.total_price.amount
                item_price = (sub * ratio).quantize(Decimal("0.01"))
                item_tax = ((tot - sub) * ratio).quantize(Decimal("0.01"))
                # Zero-value items are kept as 0-amount lines on the invoice
                if item_price < 0:
                    continue
                # ponytail: use config units/unit_price when available
                if config.units and config.units > 0:
                    qty = config.units
                    up = (item_price / qty).quantize(Decimal("0.01"))
                else:
                    qty = Decimal("1")
                    up = item_price
                items_initial.append({
                    'source_id': f"config_{config.id}",
                    'description': f"{config.name} (Q#{quote.id})",
                    'quantity': qty,
                    'unit_price': up,
                    'discount_percentage': Decimal("0.00"),
                    '_tax_amount': item_tax,
                })

            for item in quote.items.all():
                item_price = (item.line_total.amount * ratio).quantize(Decimal("0.01"))
                if item_price < 0:
                    continue
                items_initial.append({
                    'source_id': f"quote_item_{item.id}",
                    'description': f"{item.material} x{item.quantity} (Q#{quote.id})",
                    'quantity': Decimal("1"),
                    'unit_price': item_price,
                    'discount_percentage': Decimal("0.00"),
                    '_tax_amount': Decimal("0.00"),
                })

            # Inventory items attached to the quote
            # Unlike configurations, inventory items are billed at full price or not at all.
            for inv_item in quote.inventory_items.select_related("inventory_item").all():
                line_amt = inv_item.line_total.amount if inv_item.line_total else Decimal("0.00")
                tax_amt = (
                    (inv_item.total_with_tax.amount - line_amt)
                    if inv_item.total_with_tax
                    else Decimal("0.00")
                )
                if line_amt < 0:
                    continue
                inv_name = (
                    inv_item.inventory_item.name
                    if inv_item.inventory_item_id
                    else f"Inventory Item #{inv_item.pk}"
                )
                items_initial.append({
                    'source_id': f"inv_item_{inv_item.id}",
                    'description': f"{inv_name} (Q#{quote.id})",
                    'quantity': inv_item.quantity,
                    'unit_price': inv_item.unit_price.amount,
                    'discount_percentage': inv_item.discount,
                    '_tax_amount': tax_amt,
                })

        for config_data in summary.get('direct_configurations', []):
            sub = config_data['subtotal']
            tot = config_data['total']
            item_price = (sub * ratio).quantize(Decimal("0.01"))
            item_tax = ((tot - sub) * ratio).quantize(Decimal("0.01"))
            if item_price < 0:
                continue
            # ponytail: use config units/unit_price when available
            units = config_data.get('units')
            if units and units > 0:
                qty = units
                up = (item_price / qty).quantize(Decimal("0.01"))
            else:
                qty = Decimal("1")
                up = item_price
            items_initial.append({
                'source_id': f"direct_{config_data['id']}",
                'description': config_data['name'],
                'quantity': qty,
                'unit_price': up,
                'discount_percentage': Decimal("0.00"),
                '_tax_amount': item_tax,
            })

        # Work entries scale like configurations; their tax comes from the
        # entry's own AppliedTax rows (already summarised as tax_amount).
        for entry_data in summary.get('work_entries', []):
            item_price = (entry_data['total'] * ratio).quantize(Decimal("0.01"))
            item_tax = (entry_data['tax_amount'] * ratio).quantize(Decimal("0.01"))
            if item_price < 0:
                continue
            qty = entry_data.get('quantity')
            if qty and qty > 0:
                up = (item_price / qty).quantize(Decimal("0.01"))
            else:
                qty = Decimal("1")
                up = item_price
            items_initial.append({
                'source_id': f"work_entry_{entry_data['id']}",
                'description': entry_data['description'],
                'quantity': qty,
                'unit_price': up,
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

        items_data = []
        for form in items_formset:
            if not form.cleaned_data or form.cleaned_data.get('DELETE'):
                continue
            items_data.append({
                'source_id': form.cleaned_data.get('source_id', ''),
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


