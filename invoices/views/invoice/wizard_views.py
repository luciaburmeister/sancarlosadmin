from django.shortcuts import redirect
from django.urls import reverse
from unfold.views import UnfoldModelAdminViewMixin
from formtools.wizard.views import SessionWizardView
from invoices.models import SalesInvoice, SalesInvoiceItem, AppliedTax
from django.contrib.contenttypes.models import ContentType
from invoices.forms.wizard_forms import (
    SalesInvoiceBasicForm, 
    SalesInvoiceNotesForm, 
    WizardItemForm, 
    WizardTaxForm,
    WizardItemFormsetHelper,
    WizardTaxFormsetHelper
)
from django.forms import formset_factory
from django.utils.translation import gettext_lazy as _

class SalesInvoiceWizardView(UnfoldModelAdminViewMixin, SessionWizardView):
    form_list = [
        ("basic", SalesInvoiceBasicForm),
        ("items", formset_factory(WizardItemForm, extra=1, can_delete=True)),
        ("taxes", formset_factory(WizardTaxForm, extra=1, can_delete=True)),
        ("notes", SalesInvoiceNotesForm)
    ]
    template_name = "invoices/wizard_form.html"
    permission_required = ('invoices.add_salesinvoice')
    title = _("Create Sales Invoice")

    def get_form_kwargs(self, step):
        kwargs = super().get_form_kwargs(step)
        if step in ["basic", "notes"]:
            kwargs["request"] = self.request
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['prefix'] = self.steps.current

        if self.steps.current == "basic":
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
        basic_form = form_list[0]
        items_formset = form_list[1]
        taxes_formset = form_list[2]
        notes_form = form_list[3]

        # 1. Save Basic Invoice
        invoice = basic_form.save(commit=False)
        invoice.notes = notes_form.cleaned_data.get('notes', '')
        # We need to calculate subtotal from items before saving if possible, 
        # but recalculate_totals handles it. Oh wait, recalculate_totals expects subtotal_amount
        # to be calculated manually or from items. Normally, recalculate_totals doesn't calculate subtotal,
        # so we must calculate subtotal from items.
        invoice.save()

        # 2. Save Items and compute subtotal
        subtotal = 0
        for form in items_formset:
            if not form.cleaned_data or form.cleaned_data.get('DELETE'):
                continue
            item = SalesInvoiceItem.objects.create(
                invoice=invoice,
                description=form.cleaned_data.get('description'),
                quantity=form.cleaned_data.get('quantity'),
                unit_price=form.cleaned_data.get('unit_price'),
                discount_percentage=form.cleaned_data.get('discount_percentage', 0),
            )
            subtotal += item.line_total.amount # amount from Money object

        invoice.subtotal_amount = subtotal
        invoice.save(update_fields=['subtotal_amount'])

        # 3. Save Taxes
        ct = ContentType.objects.get_for_model(invoice)
        order = 0
        for form in taxes_formset:
            if not form.cleaned_data or form.cleaned_data.get('DELETE'):
                continue
            tax_type = form.cleaned_data.get('tax_type')
            selected_percentage = form.cleaned_data.get('selected_percentage')
            
            AppliedTax.objects.create(
                content_type=ct,
                object_id=invoice.id,
                tax_type=tax_type,
                selected_percentage=selected_percentage,
                order=order
            )
            order += 1

        # 4. Final totals
        invoice.recalculate_totals()

        url = reverse(
            "admin:invoices_salesinvoice_change",
            args=[invoice.id]
        )
        return redirect(url)
