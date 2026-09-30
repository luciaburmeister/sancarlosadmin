from decimal import Decimal, InvalidOperation
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, get_object_or_404
from django.urls import reverse
from django.contrib import messages
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from django.forms import formset_factory
from django.views import View

from unfold.views import UnfoldModelAdminViewMixin
from formtools.wizard.views import SessionWizardView

from invoices.models import SalesInvoice, SalesInvoiceItem, AppliedTax
from django.contrib.contenttypes.models import ContentType
from invoices.forms.wizard_forms import (
    WizardItemForm,
    WizardItemFormsetHelper,
)
from invoices.forms.project_file_wizard_forms import (
    AdvanceDeductionForm,
    ProjectFileInvoiceSetupForm,
    ProjectFileInvoiceNotesForm,
)
from general.email import sender_cc, sender_reply_to
from invoices.services.advances import AdvanceDeductionService, AdvanceInvoiceService
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from invoices.services.sales_invoice_service import SalesInvoiceService


def _is_advance(wizard):
    setup_data = wizard.get_cleaned_data_for_step("setup") or {}
    return setup_data.get('invoice_mode') == 'advance'


def _deducts_advances(wizard):
    """A goods invoice of an expediente with advances it can deduct."""
    return (
        not _is_advance(wizard)
        and wizard.invoicing_summary['advances_deductible_net'] > 0
    )


class ProjectFileInvoiceWizardView(UnfoldModelAdminViewMixin, SessionWizardView):
    form_list = [
        ("setup", ProjectFileInvoiceSetupForm),
        ("items", formset_factory(WizardItemForm, extra=1, can_delete=True)),
        # Kept apart from the goods lines: deductions are never items
        ("deductions", formset_factory(AdvanceDeductionForm, extra=0)),
        ("notes", ProjectFileInvoiceNotesForm),
    ]
    # An advance has no goods lines to edit: its lines come from the setup
    condition_dict = {
        "items": lambda wizard: not _is_advance(wizard),
        "deductions": _deducts_advances,
    }
    template_name = "invoices/project_file_invoice_wizard.html"
    permission_required = ("invoices.add_salesinvoice",)
    title = _("Create Invoice from Project File")

    def dispatch(self, request, *args, **kwargs):
        # Scoped like the expediente's change page: another company's is not found
        self.project_file = get_object_or_404(
            self.model_admin.get_queryset(request), pk=kwargs.get('project_file_id')
        )
        self.invoicing_summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self, step):
        kwargs = super().get_form_kwargs(step)
        if step == "setup":
            kwargs["request"] = self.request
            kwargs["project_file"] = self.project_file
            kwargs["invoicing_summary"] = self.invoicing_summary
        elif step == "deductions":
            kwargs["form_kwargs"] = {
                "rows": {row.line.pk: row for row in self._deduction_plan()},
            }
        elif step == "notes":
            kwargs["request"] = self.request
            kwargs["advance"] = _is_advance(self)
        return kwargs

    @staticmethod
    def _items_data(rows):
        """Goods line dicts from the items step's cleaned rows."""
        return [
            {
                'source_id': row.get('source_id', ''),
                'description': row.get('description', ''),
                'quantity': row.get('quantity', Decimal("1")),
                'unit_price': row.get('unit_price'),
                'discount_percentage': row.get('discount_percentage', Decimal("0.00")),
            }
            for row in rows
            if row and not row.get('DELETE')
        ]

    def _deduction_plan(self):
        """The default deduction plan of the goods entered in the items step
        (a dry run; nothing is written)."""
        if not hasattr(self, "_deduction_plan_rows"):
            setup_data = self.get_cleaned_data_for_step("setup") or {}
            items_data = self._items_data(self.get_cleaned_data_for_step("items") or [])
            self._deduction_plan_rows = (
                AdvanceDeductionService.preview_plan(
                    self.project_file, items_data,
                    company=setup_data['company'], client=self.project_file.client,
                )
                if setup_data.get('company') and items_data else []
            )
        return self._deduction_plan_rows

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
        if step == "deductions":
            return [
                {'advance_line': row.line.pk, 'amount': row.amount}
                for row in self._deduction_plan()
            ]

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
            context["advance_preview_url"] = reverse(
                "admin:management_projectfile_advance_preview", args=[self.project_file.id]
            )
        elif self.steps.current == "items":
            helper = WizardItemFormsetHelper()
            context["helper"] = helper
            context["formset_id"] = helper.formset_id
            context["form_id"] = helper.form_id
            context["form_add"] = helper.form_add
        elif self.steps.current == "notes":
            context["helper"] = context["form"].helper
            if _is_advance(self):
                # What will be issued, as the server rounds it
                context["advance_preview"] = AdvanceInvoiceService.preview(
                    self.project_file,
                    summary=self.invoicing_summary,
                    **ProjectFileInvoiceSetupForm.advance_input(
                        self.get_cleaned_data_for_step("setup")
                    ),
                )

        return context

    def done(self, form_list, form_dict, **kwargs):
        setup_form = form_dict["setup"]
        notes_form = form_dict["notes"]
        if setup_form.cleaned_data.get('invoice_mode') == 'advance':
            return self._done_advance(setup_form, notes_form)
        # Goods come from the items step only; deductions never go through it
        items_data = self._items_data(form.cleaned_data for form in form_dict["items"])
        # Keyed by advance line; apply() checks them against its own plan.
        # Without the step (nothing deductible when it started) the default plan.
        amounts = None
        if "deductions" in form_dict:
            amounts = {
                form.cleaned_data['advance_line']: form.cleaned_data['amount']
                for form in form_dict["deductions"]
                if form.cleaned_data
            }

        try:
            with transaction.atomic():
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
                AdvanceDeductionService.apply(invoice, amounts)

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
            messages.error(self.request, " ".join(e.messages))
            return redirect(
                reverse("admin:management_projectfile_change", args=[self.project_file.id])
            )

    def _done_advance(self, setup_form, notes_form):
        """Issue the advance entered in the setup step, as a pro forma or
        straight away as final."""
        setup_data = setup_form.cleaned_data
        try:
            with transaction.atomic():
                invoice = AdvanceInvoiceService.create(
                    project_file=self.project_file,
                    date_issued=setup_data['date_issued'],
                    due_date=setup_data.get('due_date'),
                    notes=notes_form.cleaned_data.get('notes', ''),
                    payment_method=setup_data.get('payment_method'),
                    **ProjectFileInvoiceSetupForm.advance_input(setup_data),
                )
                if notes_form.cleaned_data.get('finalize'):
                    SalesInvoiceService.finalize_invoice(
                        invoice,
                        reply_to=sender_reply_to(self.request.user),
                        cc_emails=sender_cc(self.request.user),
                    )
        except ValidationError as e:
            messages.error(self.request, " ".join(e.messages))
            return redirect(
                reverse("admin:management_projectfile_change", args=[self.project_file.id])
            )

        messages.success(
            self.request,
            _("Sales Invoice created successfully from %(ref)s.") % {
                'ref': self.project_file.reference
            }
        )
        return redirect(reverse("admin:invoices_salesinvoice_change", args=[invoice.id]))


class ProjectFileAdvancePreviewView(UnfoldModelAdminViewMixin, View):
    """GET ?percentage= or ?amount= → AdvanceInvoiceService.preview() as
    JSON, for the invoice wizard's live preview: all rounding stays on the
    server."""

    permission_required = ("invoices.add_salesinvoice",)

    def get(self, request, project_file_id):
        project_file = get_object_or_404(
            self.model_admin.get_queryset(request), pk=project_file_id
        )
        amount = self._decimal(request.GET.get("amount"))
        percentage = None if amount is not None else self._decimal(request.GET.get("percentage"))
        return JsonResponse(
            AdvanceInvoiceService.preview(project_file, percentage=percentage, amount=amount)
        )

    @staticmethod
    def _decimal(value):
        """A typed number (comma or dot decimals); None when empty, 0 when
        not a usable number, which the preview reports as invalid."""
        value = (value or "").strip().replace(",", ".")
        if not value:
            return None
        try:
            number = Decimal(value)
        except InvalidOperation:
            return Decimal("0")
        if not number.is_finite() or abs(number) >= Decimal("1e12"):
            return Decimal("0")
        return number
