from django import forms
from django.contrib import messages
from django.core.validators import FileExtensionValidator
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from general.onboarding import OK, WARNING, Check, Column, ImportPrompt, OnboardingStep, Section, register
from general.onboarding.forms import UnfoldFormMixin
from invoices.models import Invoice, InvoicePaymentStatus, SalesInvoice

SALES, PURCHASES = "sales", "purchases"

# Row errors shown after an import; the rest are summed up
SHOWN_ERRORS = 10


class OpenInvoicesImportForm(UnfoldFormMixin, forms.Form):
    kind = forms.ChoiceField(
        label=gettext_lazy("Invoices"),
        choices=[(SALES, gettext_lazy("Sales: what clients owe you")),
                 (PURCHASES, gettext_lazy("Purchases: what you owe providers"))],
    )
    file = forms.FileField(
        label=gettext_lazy("Excel file"),
        validators=[FileExtensionValidator(allowed_extensions=["xlsx"])],
        help_text=gettext_lazy(
            "One row per invoice with invoice_number, date_issued, base_amount, iva_percentage, iva_amount, "
            "total and client_cif / client_name (sales) or provider_cif / provider_name (purchases). "
            "A company column, if any, is ignored."
        ),
    )
    default_year = forms.IntegerField(
        label=gettext_lazy("Default year"), required=False,
        help_text=gettext_lazy("For dates written without a year. Leave blank for the current year."),
    )
    multi_iva = forms.BooleanField(
        label=gettext_lazy("Several IVA rates per invoice"), required=False,
        help_text=gettext_lazy(
            "Purchases only: rows with the same number and provider are one invoice, one row per IVA rate. "
            "Sales invoices are always grouped this way."
        ),
    )


@register
class OpenInvoicesStep(OnboardingStep):
    key = "open-invoices"
    title = gettext_lazy("Open invoices")
    description = gettext_lazy(
        "Invoices from your previous system still to be paid or collected, so payments and bank "
        "movements can be matched to them. They keep their numbers; yours start afresh in your own format."
    )
    order = 90
    required = False
    form_class = OpenInvoicesImportForm
    submit_label = gettext_lazy("Import")

    def sections(self):
        open_statuses = [InvoicePaymentStatus.UNPAID, InvoicePaymentStatus.PARTIAL]
        sales = SalesInvoice.objects.filter(
            company=self.company, is_external=True, payment_status__in=open_statuses,
        ).count()
        purchases = Invoice.objects.filter(company=self.company, payment_status__in=open_statuses).count()
        return [Section(_("Open invoices"), [
            Check(_("To collect"), OK if sales else WARNING,
                  _("%(count)s imported.") % {"count": sales} if sales else _("None imported.")),
            Check(_("To pay"), OK if purchases else WARNING,
                  _("%(count)s unpaid.") % {"count": purchases} if purchases else _("None imported.")),
        ])]

    def import_prompts(self):
        def invoice_prompt(title, party, party_cif, party_name, grouping, extra_columns=()):
            return ImportPrompt(
                title=title,
                rows=_("one row per invoice"),
                columns=[
                    Column("invoice_number", _("the invoice number, as printed on it."), required=True),
                    Column("date_issued", _("the issue date, as an Excel date or DD/MM/YYYY."), required=True),
                    *extra_columns,
                    Column(party_cif, _("the %(party)s's tax identifier (NIF/CIF/VAT number), without spaces "
                                        "or dashes.") % {"party": party}, required=True),
                    Column(party_name, _("the %(party)s's name.") % {"party": party}),
                    Column("description", _("what the invoice is for, in a few words.")),
                    Column("base_amount", _("the taxable base, before IVA and IRPF."), required=True),
                    Column("iva_percentage", _("the IVA rate, e.g. 21 (not 0.21).")),
                    Column("iva_amount", _("the IVA amount.")),
                    Column("retention", _("the IRPF withheld, as a positive amount; empty or 0 when there is none.")),
                    Column("total", _("the invoice total: base + IVA − IRPF.")),
                ],
                rules=[
                    _("Only include invoices still pending payment. If one is partly paid, give its full "
                      "amounts anyway and list it at the end, so I can record what was already paid."),
                    grouping,
                ],
            )

        return [
            invoice_prompt(
                _("Sales invoices: what clients owe you"), _("client"), "client_cif", "client_name",
                _("If an invoice has several IVA rates, write one row per rate, repeating invoice_number "
                  "and client_cif, each row with that rate's base and amounts."),
                extra_columns=[Column("due_date", _("when it falls due, as an Excel date or DD/MM/YYYY."))],
            ),
            invoice_prompt(
                _("Purchase invoices: what you owe providers"), _("provider"), "provider_cif", "provider_name",
                _("If an invoice has several IVA rates, write one row per rate, repeating invoice_number "
                  "and provider_cif, each row with that rate's base and amounts. (Then tick \"Several IVA "
                  "rates per invoice\" when importing.)"),
            ),
        ]

    def save(self, form, request):
        from invoices.services.import_sales_invoice_service import import_sales_invoices_from_excel
        from invoices.services.invoice.import_invoice_service import import_invoices_from_excel

        data = form.cleaned_data
        if data["kind"] == SALES:
            result = import_sales_invoices_from_excel(
                data["file"], default_year=data["default_year"], company=self.company,
            )
        else:
            result = import_invoices_from_excel(
                data["file"], default_year=data["default_year"], multi_iva_mode=data["multi_iva"],
                company=self.company,
            )

        errors = [row for row in result.rows if row.status == "error"]
        for row in errors[:SHOWN_ERRORS]:
            messages.error(request, _("Row %(row)s: %(message)s") % {"row": row.row_number, "message": row.message})
        if len(errors) > SHOWN_ERRORS:
            messages.error(request, _("…and %(count)s more rows with errors.") % {"count": len(errors) - SHOWN_ERRORS})
        return _("%(created)s invoices imported, %(skipped)s skipped, %(errors)s with errors.") % {
            "created": result.created, "skipped": result.skipped, "errors": result.errors,
        }
