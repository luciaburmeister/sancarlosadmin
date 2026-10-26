"""Shared candidate querysets for bank transaction reconciliation.

Each function returns the base queryset of documents eligible to match a
transaction with the given company/cutoff_date, with no select_related,
ordering, or annotation applied - callers chain whatever they need on top
(TransactionMatcher annotates with annotate_amount_remaining for its amount
comparisons; ManualReconcileView adds select_related/order_by for display).
"""
from django.apps import apps
from django.contrib.contenttypes.models import ContentType


def invoice_candidates(company, cutoff_date, counterparty=None):
    Invoice = apps.get_model("invoices", "Invoice")
    qs = Invoice.objects.filter(
        company=company,
        payment_status__in=["unpaid", "partial"],
        date_issued__lte=cutoff_date,
    )
    if counterparty:
        qs = qs.filter(provider=counterparty)
    return qs


def sales_invoice_candidates(company, cutoff_date, counterparty=None):
    SalesInvoice = apps.get_model("invoices", "SalesInvoice")
    qs = SalesInvoice.objects.filter(
        company=company,
        payment_status__in=["unpaid", "partial"],
        date_issued__lte=cutoff_date,
    )
    if counterparty:
        qs = qs.filter(client=counterparty)
    return qs


def loan_installment_candidates(company, bank_account, cutoff_date):
    LoanInstallment = apps.get_model("loans", "LoanInstallment")
    return LoanInstallment.objects.filter(
        loan__company=company,
        loan__bank_account=bank_account,
        payment_status__in=["unpaid", "partial"],
        due_date__lte=cutoff_date,
    )


def loan_event_candidates(company, bank_account, cutoff_date):
    LoanEvent = apps.get_model("loans", "LoanEvent")
    from payments.models import PaymentAllocation

    ct = ContentType.objects.get_for_model(LoanEvent)
    reconciled_event_ids = PaymentAllocation.objects.filter(
        content_type=ct
    ).values_list("object_id", flat=True)
    return LoanEvent.objects.filter(
        parent_loan__bank_account=bank_account,
        parent_loan__company=company,
        event_type="early_repayment",
        date__lte=cutoff_date,
    ).exclude(pk__in=reconciled_event_ids)


def expense_claim_candidates(company, cutoff_date, employee=None):
    ExpenseClaim = apps.get_model("expenses", "ExpenseClaim")
    from apps.expenses.models import ExpenseClaimStatus

    qs = ExpenseClaim.objects.filter(
        company=company,
        status=ExpenseClaimStatus.APPROVED,
        created_at__date__lte=cutoff_date,
    )
    if employee and employee.user_id:
        qs = qs.filter(submitted_by_id=employee.user_id)
    return qs


def employee_payroll_candidates(company, cutoff_date):
    from django.db.models import Q

    EmployeePayroll = apps.get_model("payroll", "EmployeePayroll")
    return EmployeePayroll.objects.filter(
        company=company,
        is_paid=False,
    ).filter(
        Q(period__year__lt=cutoff_date.year)
        | Q(period__year=cutoff_date.year, period__month__lte=cutoff_date.month)
    )


def tax_installment_candidates(company, cutoff_date, refund=False):
    """Installments we owe the tax authority, or (refund=True) that it owes us."""
    TaxInstallment = apps.get_model("finance", "TaxInstallment")
    return TaxInstallment.objects.filter(
        planned_tax_payment__company=company,
        planned_tax_payment__direction="refund" if refund else "payment",
        payment_status__in=["unpaid", "partial"],
        due_date__lte=cutoff_date,
    )
