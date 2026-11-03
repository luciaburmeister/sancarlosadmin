from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext as _, gettext_lazy
from django.views.generic.edit import FormView
from unfold.views import UnfoldModelAdminViewMixin

from invoices import sepa
from invoices.forms.invoice.sepa_direct_debit_form import SepaDirectDebitForm


class SepaDirectDebitAdminView(UnfoldModelAdminViewMixin, FormView):
    """
    Generate the SEPA direct debit file for the sales invoices selected in
    the list (``?ids=1,2,3``): shows which ones will be collected and why
    the others will not, then downloads the XML to upload to the bank.
    """
    form_class = SepaDirectDebitForm
    title = gettext_lazy("SEPA direct debit file")
    permission_required = ("invoices.change_salesinvoice",)
    template_name = "admin/invoices/salesinvoice/sepa_direct_debit.html"

    def dispatch(self, request, *args, **kwargs):
        if not self.has_permission():
            return self.handle_no_permission()
        ids = [pk for pk in request.GET.get("ids", "").split(",") if pk.isdigit()]
        self.invoices = list(
            self.model_admin.get_queryset(request)
            .filter(pk__in=ids)
            .select_related("company", "client")
            .prefetch_related("client__client_bank_accounts")
            .order_by("client__name", "due_date")
        )
        changelist = reverse("admin:invoices_salesinvoice_changelist")
        if not self.invoices:
            messages.error(request, _("Select at least one invoice."))
            return redirect(changelist)
        companies = {invoice.company for invoice in self.invoices}
        if len(companies) > 1:
            messages.error(request, _("Select invoices of a single company: a SEPA file collects into one company's account."))
            return redirect(changelist)
        self.company = companies.pop()
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "company": self.company}

    def get_context_data(self, **kwargs):
        rows = []
        for invoice in self.invoices:
            account, reason = sepa.check_invoice(invoice, self.company)
            rows.append({"invoice": invoice, "account": account, "reason": reason})
        return super().get_context_data(
            **kwargs,
            company=self.company,
            rows=rows,
            collectable_count=sum(1 for row in rows if not row["reason"]),
        )

    def form_valid(self, form):
        collection_date = form.cleaned_data["collection_date"]
        scheme = form.cleaned_data["scheme"]
        try:
            xml, _skipped = sepa.build_direct_debit_xml(
                self.invoices,
                creditor_account=form.cleaned_data["creditor_account"],
                collection_date=collection_date,
                scheme=scheme,
            )
        except ValidationError as e:
            form.add_error(None, e)
            return self.form_invalid(form)
        response = HttpResponse(xml, content_type="application/xml")
        response["Content-Disposition"] = f'attachment; filename="SEPA_{scheme}_{collection_date:%Y%m%d}.xml"'
        return response
