"""Views for early repayment — closes active tranche and opens a new child loan."""
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView

from unfold.views import UnfoldModelAdminViewMixin
from unfold.widgets import (
    UnfoldAdminDateWidget,
    UnfoldAdminDecimalFieldWidget,
)

from loans.models import Loan, LoanEvent, LoanEventType, LoanStatus, ParentLoan


class EarlyRepaymentForm(forms.Form):
    extra_principal = forms.DecimalField(
        label=_("Extra principal payment"),
        min_value=Decimal("0.01"),
        decimal_places=2,
        max_digits=14,
        widget=UnfoldAdminDecimalFieldWidget(),
        help_text=_(
            "Amount of extra capital you are paying to the bank on top of the regular "
            "installment. This will close the current tranche and open a new one with "
            "the remaining balance minus this amount."
        ),
    )
    repayment_date = forms.DateField(
        label=_("Repayment date"),
        widget=UnfoldAdminDateWidget(),
        help_text=_(
            "Date the extra payment was made. Used as the start date of the new tranche."
        ),
    )


class EarlyRepaymentView(UnfoldModelAdminViewMixin, FormView):
    title = _("Register Early Repayment")
    permission_required = ("loans.change_parentloan",)
    template_name = "admin/loans/early_repayment.html"
    form_class = EarlyRepaymentForm

    def get_parent_loan(self):
        return get_object_or_404(ParentLoan, pk=self.kwargs["pk"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        parent = self.get_parent_loan()
        active = parent.active_tranche
        context.update({
            "parent_loan": parent,
            "active_tranche": active,
            "outstanding": active.outstanding_principal if active else None,
            "back_url": reverse("admin:loans_parentloan_change", args=[parent.pk]),
        })
        return context

    def form_valid(self, form):
        parent = self.get_parent_loan()
        active = parent.active_tranche

        if not active:
            messages.error(self.request, _("No active tranche found for this loan."))
            return redirect(reverse("admin:loans_parentloan_change", args=[parent.pk]))

        extra = form.cleaned_data["extra_principal"]
        repayment_date = form.cleaned_data["repayment_date"]
        outstanding = active.outstanding_principal
        outstanding_amount = (
            outstanding.amount if hasattr(outstanding, "amount")
            else Decimal(str(outstanding))
        )
        currency = active.principal_amount_currency

        if extra >= outstanding_amount:
            messages.error(
                self.request,
                _(
                    "Extra payment (%(extra)s) must be less than the outstanding "
                    "principal (%(outstanding)s)."
                ) % {"extra": extra, "outstanding": outstanding_amount},
            )
            return self.form_invalid(form)

        new_principal = outstanding_amount - extra
        remaining_months = active.installments.filter(
            due_date__gt=repayment_date
        ).count()

        if remaining_months == 0:
            messages.error(
                self.request,
                _("No remaining installments after the repayment date.")
            )
            return self.form_invalid(form)

        with transaction.atomic():
            # 1. Settle current tranche
            active.status = LoanStatus.SETTLED
            active.save(update_fields=["status"])

            # 2. Create LoanEvent for this early repayment
            # This event implements Payable and can be reconciled
            # against the real BankTransaction via the matcher
            LoanEvent.objects.create(
                parent_loan=parent,
                event_type=LoanEventType.EARLY_REPAYMENT,
                date=repayment_date,
                amount=extra,
                amount_currency=currency,
                notes=_(
                    "Auto-created early repayment event. "
                    "Reconcile against the bank transaction to confirm."
                ),
            )

            # 3. Open new tranche with remaining principal
            new_tranche = Loan.objects.create(
                parent_loan=parent,
                company=parent.company,
                bank=parent.bank,
                bank_account=parent.bank_account,
                name=active.name,
                reference=active.reference,
                principal_amount=new_principal,
                principal_amount_currency=currency,
                interest_rate=active.interest_rate,
                interest_rate_type=active.interest_rate_type,
                amortization_method=active.amortization_method,
                interest_day_count=active.interest_day_count,
                start_date=repayment_date,
                term_months=remaining_months,
                payment_frequency=active.payment_frequency,
                status=LoanStatus.ACTIVE,
                notes=active.notes,
            )

        messages.success(
            self.request,
            _(
                "Early repayment registered. Tranche #%(old)d settled. "
                "New tranche #%(new)d created with principal %(principal)s %(currency)s "
                "over %(months)d months. "
                "Reconcile the bank transaction to confirm the payment."
            ) % {
                "old": active.pk,
                "new": new_tranche.pk,
                "principal": new_principal,
                "currency": currency,
                "months": remaining_months,
            },
        )
        return redirect(reverse("admin:loans_parentloan_change", args=[parent.pk]))