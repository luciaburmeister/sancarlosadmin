"""Admin screens sending purchase invoice payments from the bank (see BankPaymentService)."""
import logging
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from unfold.widgets import UnfoldAdminDecimalFieldWidget, UnfoldAdminSelectWidget

from banking.models import BankPaymentOrder, BankPaymentOrderStatus
from banking.services.bank_payments import BankPaymentService
from core.admin.company_access import get_user_companies
from invoices.models import Invoice

logger = logging.getLogger(__name__)


class PayFromBankForm(forms.Form):
    bank_account = forms.ModelChoiceField(
        queryset=None,
        label=_("From bank account"),
        help_text=_("Only connected accounts with a valid consent. You sign the transfer at the bank next."),
        widget=UnfoldAdminSelectWidget,
    )

    def __init__(self, *args, company, invoices, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["bank_account"].queryset = BankPaymentService.source_accounts(company)
        self.invoices = invoices
        for invoice in invoices:
            self.fields[self.amount_field(invoice)] = forms.DecimalField(
                max_digits=12,
                decimal_places=2,
                min_value=Decimal("0.01"),
                max_value=invoice.amount_remaining.amount,
                initial=invoice.amount_remaining.amount,
                label=invoice.invoice_number or str(invoice.pk),
                widget=UnfoldAdminDecimalFieldWidget,
            )

    @staticmethod
    def amount_field(invoice):
        return f"amount_{invoice.pk}"

    def rows(self):
        for invoice in self.invoices:
            yield {
                "invoice": invoice,
                "destination": BankPaymentService.destination_account(invoice),
                "concept": BankPaymentService.concept(invoice),
                "amount": self[self.amount_field(invoice)],
            }

    def amounts(self):
        return {invoice: self.cleaned_data[self.amount_field(invoice)] for invoice in self.invoices}


class PayInvoicesFromBankView(View):
    """GET/POST ?ids=1,2,3 — one invoice goes as a single transfer, several as one bulk order."""

    model_admin = None
    template_name = "admin/banking/bankpaymentorder/pay_invoices.html"

    def dispatch(self, request, *args, **kwargs):
        if not request.user.has_perm("banking.can_initiate_bank_payment"):
            raise PermissionDenied
        ids = [i for i in (request.GET.get("ids") or "").split(",") if i.strip().isdigit()]
        self.back_url = reverse("admin:invoices_invoice_changelist")
        if len(ids) == 1:
            self.back_url = reverse("admin:invoices_invoice_change", args=[ids[0]])
        invoices = list(
            Invoice.objects.filter(pk__in=ids, company__in=get_user_companies(request.user))
            .select_related("provider", "company")
            .order_by("provider__name", "due_date", "pk")
        )
        if not invoices:
            messages.error(request, _("Select at least one invoice to pay."))
            return redirect(self.back_url)
        companies = {invoice.company_id for invoice in invoices}
        if len(companies) > 1:
            messages.error(request, _("A bank payment leaves from one company's account: select invoices of one company."))
            return redirect(self.back_url)
        self.company = invoices[0].company
        if not BankPaymentService.can_pay_from_bank(request.user, self.company):
            messages.error(
                request,
                _("%(company)s has no connected bank account with a valid consent to pay from.") % {"company": self.company},
            )
            return redirect(self.back_url)
        self.payable, self.refused = BankPaymentService.split_eligible(invoices)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        return self._render(request, self._form())

    def post(self, request):
        form = self._form(request.POST)
        if form.is_valid() and self.payable:
            try:
                order = BankPaymentService.initiate(
                    user=request.user,
                    bank_account=form.cleaned_data["bank_account"],
                    amounts=form.amounts(),
                    return_url_for=lambda order: request.build_absolute_uri(
                        reverse("admin:banking_bankpaymentorder_return", args=[order.token])
                    ),
                )
            except ValidationError as e:
                form.add_error(None, e)
            else:
                return redirect(order.follow_url)
        return self._render(request, form)

    def _form(self, data=None):
        default = BankPaymentService.source_accounts(self.company).first()
        return PayFromBankForm(
            data, company=self.company, invoices=self.payable, initial={"bank_account": default},
        )

    def _render(self, request, form):
        context = {
            **self.model_admin.admin_site.each_context(request),
            "form": form,
            "rows": list(form.rows()),
            "refused": self.refused,
            "company": self.company,
            "cancel_url": self.back_url,
            "title": _("Pay from the bank"),
            "opts": BankPaymentOrder._meta,
        }
        return TemplateResponse(request, self.template_name, context)


class BankPaymentReturnView(View):
    """Where the bank sends the user back after signing (or abandoning) an order."""

    model_admin = None

    def get(self, request, token):
        if not request.user.has_perm("banking.can_initiate_bank_payment"):
            raise PermissionDenied
        order = get_object_or_404(
            BankPaymentOrder.objects.filter(company__in=get_user_companies(request.user)), token=token,
        )
        try:
            order = BankPaymentService.refresh_status(order)
        except Exception:
            logger.exception("Could not refresh bank payment order %s on return", order.pk)
        if order.status == BankPaymentOrderStatus.COMPLETED:
            messages.success(
                request,
                _("The bank has accepted the payment: the invoices are marked paid and the movement will be matched when it syncs."),
            )
        elif order.is_pending:
            messages.info(
                request,
                _("The bank has not confirmed the payment yet. Its status is checked again automatically."),
            )
        else:
            messages.warning(
                request,
                _("The payment was not made: %(status)s.") % {"status": order.get_status_display()},
            )
        return redirect(reverse("admin:banking_bankpaymentorder_change", args=[order.pk]))
