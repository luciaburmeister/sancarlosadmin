from datetime import date, timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.db.models import (
    DecimalField,
    F,
    Sum,
    Value,
    Count,
)
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from core.admin.company_access import get_user_companies
from invoices.forms.operational_dashboard_filters import OperationalDashboardFilterForm
from invoices.models import AppliedTax, Invoice, InvoicePaymentStatus, SalesInvoice, SalesInvoiceStatus

from production.models import (
            ProductionTicket,
            ProductionItemSession,
            ProductionItemSessionStatus,
        )
from configurator.models import Configuration

_CACHE_TTL = 300


class OperationalDashboardView(UnfoldModelAdminViewMixin, TemplateView):
    """
    Operational dashboard:
    1. Invoicing without taxes — SalesInvoice taxable base (subtotal minus discount), split pro-forma / final
    2. Production costs — Configuration.total_cost for tickets worked in the period
    """

    title = _("Operational Dashboard")
    permission_required = ("invoices.operational_dashboard",)
    template_name = "admin/invoices/invoice/operational_dashboard.html"
    model_admin = None

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request

        # ── Company filtering ──────────────────────────────────────
        user_companies = get_user_companies(request.user)
        selected_company_id = request.GET.get("company")

        try:
            selected_company_id = int(selected_company_id) if selected_company_id else None
        except (ValueError, TypeError):
            selected_company_id = None

        if selected_company_id and user_companies.filter(id=selected_company_id).exists():
            selected_company = user_companies.get(id=selected_company_id)
        elif user_companies.exists():
            selected_company = user_companies.first()
        else:
            selected_company = None

        # ── Date range ─────────────────────────────────────────────
        today = timezone.localdate()
        default_from = today.replace(day=1)
        default_to = today

        filter_form = OperationalDashboardFilterForm(request.GET)
        filter_form.is_valid()
        cleaned = filter_form.cleaned_data
        date_from = cleaned.get("date_from") or default_from
        date_to = cleaned.get("date_to") or default_to

        # ── Cache ──────────────────────────────────────────────────
        company_pk = selected_company.pk if selected_company else 0
        cache_key = f"op_dash:{company_pk}:{date_from.isoformat()}:{date_to.isoformat()}"
        cached = cache.get(cache_key)

        if cached:
            invoicing_data, production_data, tax_data, payment_data, cash_data, ar_aging_data, ap_aging_data, bank_balances_data, loan_data = cached
        else:
            invoicing_data = self._compute_invoicing(selected_company, date_from, date_to)
            production_data = self._compute_production_costs(selected_company, date_from, date_to)
            tax_data = self._compute_tax_summary(selected_company, date_from, date_to)
            payment_data = self._compute_payment_invoicing(selected_company, date_from, date_to)
            cash_data = self._compute_tax_periods(selected_company, date_from, date_to)
            ar_aging_data = self._compute_ar_aging(selected_company)
            ap_aging_data = self._compute_ap_aging(selected_company)
            bank_balances_data = self._compute_bank_balances(selected_company)
            loan_data = self._compute_loan_data(selected_company)
            cache.set(cache_key, (invoicing_data, production_data, tax_data, payment_data, cash_data, ar_aging_data, ap_aging_data, bank_balances_data, loan_data), _CACHE_TTL)

        ctx.update({
            "title": _("Operational Dashboard"),
            "opts": SalesInvoice._meta,
            "companies": user_companies,
            "selected_company": selected_company,
            "filter_form": filter_form,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "invoicing": invoicing_data,
            "production": production_data,
            "taxes": tax_data,
            "payment": payment_data,
            "tax_periods": cash_data,
            "bank_balances": bank_balances_data,
            "loans": loan_data,
            "today": today,
        })
        return ctx

    # ── Invoicing helpers ──────────────────────────────────────────

    @staticmethod
    def _compute_invoicing(company, date_from, date_to):
        if not company:
            return {
                "final_total": Decimal("0.00"),
                "final_count": 0,
                "proforma_total": Decimal("0.00"),
                "proforma_count": 0,
                "grand_total": Decimal("0.00"),
                "monthly_final": [],
                "monthly_proforma": [],
                "top_clients": [],
                "final_total_with_tax": Decimal("0.00"),
                "total_paid": Decimal("0.00"),
                "pending_to_collect": Decimal("0.00"),
                "cumulative_debt": Decimal("0.00"),
                # NEW — payment status breakdown
                "paid_count": 0,
                "partial_total": Decimal("0.00"),
                "partial_count": 0,
                "unpaid_total": Decimal("0.00"),
                "unpaid_count": 0,
            }

        base_qs = SalesInvoice.objects.filter(
            company=company,
            date_issued__gte=date_from,
            date_issued__lte=date_to,
        )

        zero = Value(Decimal("0.00"))
        field = DecimalField(max_digits=12, decimal_places=2)
        # Invoicing without taxes = taxable base: line totals net of the
        # invoice-level discount.
        taxable_base = F("subtotal_amount") - F("discount_amount")

        final_agg = base_qs.filter(status=SalesInvoiceStatus.FINAL).aggregate(
            total=Coalesce(Sum(taxable_base), zero, output_field=field),
            count=Count("id"),
        )
        proforma_agg = base_qs.filter(status=SalesInvoiceStatus.PROFORMA).aggregate(
            total=Coalesce(Sum(taxable_base), zero, output_field=field),
            count=Count("id"),
        )

        monthly_final = list(
            base_qs.filter(status=SalesInvoiceStatus.FINAL)
            .annotate(month=TruncMonth("date_issued"))
            .values("month")
            .annotate(total=Coalesce(Sum(taxable_base), zero, output_field=field))
            .order_by("month")
        )

        monthly_proforma = list(
            base_qs.filter(status=SalesInvoiceStatus.PROFORMA)
            .annotate(month=TruncMonth("date_issued"))
            .values("month")
            .annotate(total=Coalesce(Sum(taxable_base), zero, output_field=field))
            .order_by("month")
        )

        top_clients = list(
            base_qs.values("client__name")
            .annotate(total=Coalesce(Sum(taxable_base), zero, output_field=field))
            .order_by("-total")[:10]
        )

        final_qs = base_qs.filter(status=SalesInvoiceStatus.FINAL)
        final_total_with_tax = final_qs.aggregate(
            total=Coalesce(Sum("total_amount"), zero, output_field=field),
        )["total"]

        sales_ct = ContentType.objects.get_for_model(SalesInvoice)
        final_ids = list(final_qs.values_list("pk", flat=True))

        from payments.models import PaymentAllocation
        total_paid = PaymentAllocation.objects.filter(
            content_type=sales_ct,
            object_id__in=final_ids,
        ).aggregate(
            total=Coalesce(Sum("amount_applied"), zero, output_field=field),
        )["total"]

        pending_to_collect = final_total_with_tax - total_paid

        all_final_qs = SalesInvoice.objects.filter(
            company=company,
            status=SalesInvoiceStatus.FINAL,
            date_issued__lte=date_to,
        ).exclude(payment_status=InvoicePaymentStatus.PAID)

        cumulative_total = all_final_qs.aggregate(
            total=Coalesce(Sum("total_amount"), zero, output_field=field),
        )["total"]

        cumulative_ids = list(all_final_qs.values_list("pk", flat=True))
        cumulative_paid = PaymentAllocation.objects.filter(
            content_type=sales_ct,
            object_id__in=cumulative_ids,
        ).aggregate(
            total=Coalesce(Sum("amount_applied"), zero, output_field=field),
        )["total"]

        cumulative_debt = cumulative_total - cumulative_paid

        # ── NEW: payment status breakdown (paid / partial / unpaid) ──
        # Scoped to the selected period (date_from → date_to), final invoices only.
        # These three queries power the three Collection Status KPI cards in the template.
        paid_agg = base_qs.filter(
            status=SalesInvoiceStatus.FINAL,
            payment_status=InvoicePaymentStatus.PAID,
        ).aggregate(
            total=Coalesce(Sum("total_amount"), zero, output_field=field),
            count=Count("id"),
        )

        partial_agg = base_qs.filter(
            status=SalesInvoiceStatus.FINAL,
            payment_status=InvoicePaymentStatus.PARTIAL,
        ).aggregate(
            total=Coalesce(Sum("total_amount"), zero, output_field=field),
            count=Count("id"),
        )

        unpaid_agg = base_qs.filter(
            status=SalesInvoiceStatus.FINAL,
            payment_status=InvoicePaymentStatus.UNPAID,
        ).aggregate(
            total=Coalesce(Sum("total_amount"), zero, output_field=field),
            count=Count("id"),
        )

        return {
            "final_total": final_agg["total"],
            "final_count": final_agg["count"],
            "proforma_total": proforma_agg["total"],
            "proforma_count": proforma_agg["count"],
            "grand_total": final_agg["total"] + proforma_agg["total"],
            "monthly_final": monthly_final,
            "monthly_proforma": monthly_proforma,
            "top_clients": top_clients,
            "final_total_with_tax": final_total_with_tax,
            "total_paid": total_paid,
            "pending_to_collect": pending_to_collect,
            "cumulative_debt": cumulative_debt,
            # NEW
            "paid_count": paid_agg["count"],
            "partial_total": partial_agg["total"],
            "partial_count": partial_agg["count"],
            "unpaid_total": unpaid_agg["total"],
            "unpaid_count": unpaid_agg["count"],
        }

    # ── Production cost helpers ─────────────────────────────────────

    @staticmethod
    def _compute_production_costs(company, date_from, date_to):
        if not company:
            return {
                "total_cost": Decimal("0.00"),
                "ticket_count": 0,
                "monthly": [],
            }

        zero = Value(Decimal("0.00"))
        field = DecimalField(max_digits=12, decimal_places=2)

        config_ct = ContentType.objects.get_for_model(Configuration)

        session_qs = ProductionItemSession.objects.filter(
            company=company,
            status=ProductionItemSessionStatus.DONE,
            start_time__date__gte=date_from,
            start_time__date__lte=date_to,
        )

        ticket_ids_with_work = set(
            session_qs.values_list(
                "production_item__ticket_id", flat=True
            )
        )

        config_ids = list(
            ProductionTicket.objects.filter(
                pk__in=ticket_ids_with_work,
                company=company,
                content_type=config_ct,
            ).values_list("object_id", flat=True).distinct()
        )

        total_cost = Configuration.objects.filter(
            pk__in=config_ids,
        ).aggregate(
            total=Coalesce(Sum("total_cost"), zero, output_field=field),
        )["total"]

        monthly_data = []
        sessions_by_month = (
            session_qs
            .annotate(month=TruncMonth("start_time"))
            .values("month")
            .annotate(
                ticket_count=Count("production_item__ticket_id", distinct=True),
            )
            .order_by("month")
        )

        for row in sessions_by_month:
            month = row["month"]
            month_ticket_ids = set(
                session_qs.filter(
                    start_time__year=month.year,
                    start_time__month=month.month,
                ).values_list("production_item__ticket_id", flat=True)
            )
            month_config_ids = list(
                ProductionTicket.objects.filter(
                    pk__in=month_ticket_ids,
                    content_type=config_ct,
                ).values_list("object_id", flat=True).distinct()
            )

            month_cost = Configuration.objects.filter(
                pk__in=month_config_ids,
            ).aggregate(
                total=Coalesce(Sum("total_cost"), zero, output_field=field),
            )["total"]

            monthly_data.append({
                "month": month,
                "total": month_cost,
            })

        ticket_count = len(ticket_ids_with_work)
        avg_cost = (total_cost / ticket_count).quantize(Decimal("0.01")) if ticket_count else Decimal("0.00")

        return {
            "total_cost": total_cost,
            "ticket_count": ticket_count,
            "avg_cost_per_ticket": avg_cost,
            "monthly": monthly_data,
        }

    # ── Tax summary helpers ─────────────────────────────────────────

    @staticmethod
    def _compute_tax_summary(company, date_from, date_to):
        """Aggregate AppliedTax by tax_type for sales (billed) and purchase (paid) invoices."""
        if not company:
            return {
                "billed": [],
                "paid": [],
                "billed_total": Decimal("0.00"),
                "paid_total": Decimal("0.00"),
                "net": Decimal("0.00"),
            }

        zero = Value(Decimal("0.00"))
        field = DecimalField(max_digits=12, decimal_places=2)

        sales_ct = ContentType.objects.get_for_model(SalesInvoice)
        purchase_ct = ContentType.objects.get_for_model(Invoice)

        sales_invoice_ids = list(
            SalesInvoice.objects.filter(
                company=company,
                date_issued__gte=date_from,
                date_issued__lte=date_to,
            ).values_list("pk", flat=True)
        )

        billed_qs = (
            AppliedTax.objects.filter(
                content_type=sales_ct,
                object_id__in=sales_invoice_ids,
            )
            .values("tax_type__tax_name")
            .annotate(
                total_base=Coalesce(Sum("base_amount"), zero, output_field=field),
                total_tax=Coalesce(Sum("calculated_amount"), zero, output_field=field),
                count=Count("id"),
            )
            .order_by("-total_tax")
        )

        billed = list(billed_qs)
        billed_total = sum(row["total_tax"] for row in billed)

        purchase_invoice_ids = list(
            Invoice.objects.filter(
                company=company,
                date_issued__gte=date_from,
                date_issued__lte=date_to,
            ).values_list("pk", flat=True)
        )

        paid_qs = (
            AppliedTax.objects.filter(
                content_type=purchase_ct,
                object_id__in=purchase_invoice_ids,
            )
            .values("tax_type__tax_name")
            .annotate(
                total_base=Coalesce(Sum("base_amount"), zero, output_field=field),
                total_tax=Coalesce(Sum("calculated_amount"), zero, output_field=field),
                count=Count("id"),
            )
            .order_by("-total_tax")
        )

        paid = list(paid_qs)
        paid_total = sum(row["total_tax"] for row in paid)

        balance_map = {}

        for row in billed:
            key = row["tax_type__tax_name"]
            balance_map.setdefault(key, {
                "tax_name": key,
                "billed_base": Decimal("0.00"),
                "billed_tax": Decimal("0.00"),
                "paid_base": Decimal("0.00"),
                "paid_tax": Decimal("0.00"),
            })
            balance_map[key]["billed_base"] += row["total_base"]
            balance_map[key]["billed_tax"] += row["total_tax"]

        for row in paid:
            key = row["tax_type__tax_name"]
            balance_map.setdefault(key, {
                "tax_name": key,
                "billed_base": Decimal("0.00"),
                "billed_tax": Decimal("0.00"),
                "paid_base": Decimal("0.00"),
                "paid_tax": Decimal("0.00"),
            })
            balance_map[key]["paid_base"] += row["total_base"]
            balance_map[key]["paid_tax"] += row["total_tax"]

        balance = []
        for entry in balance_map.values():
            entry["net"] = entry["billed_tax"] - entry["paid_tax"]
            balance.append(entry)

        balance.sort(key=lambda x: abs(x["net"]), reverse=True)

        return {
            "billed": billed,
            "paid": paid,
            "billed_total": billed_total,
            "paid_total": paid_total,
            "net": billed_total - paid_total,
            "balance": balance,
        }

    # ── Payment invoicing helpers ───────────────────────────────────

    @staticmethod
    def _compute_payment_invoicing(company, date_from, date_to):
        """Aggregate purchase invoices with due_date in the selected range."""
        if not company:
            return {
                "total": Decimal("0.00"),
                "count": 0,
                "paid_total": Decimal("0.00"),
                "paid_count": 0,
                # NEW — split partial from unpaid
                "partial_total": Decimal("0.00"),
                "partial_count": 0,
                "unpaid_total": Decimal("0.00"),
                "unpaid_count": 0,
                "monthly": [],
                "top_providers": [],
            }

        zero = Value(Decimal("0.00"))
        field = DecimalField(max_digits=12, decimal_places=2)

        base_qs = Invoice.objects.filter(
            company=company,
            due_date__gte=date_from,
            due_date__lte=date_to,
        )

        total_agg = base_qs.aggregate(
            total=Coalesce(Sum("total_amount"), zero, output_field=field),
            count=Count("id"),
        )

        paid_agg = base_qs.filter(
            payment_status=InvoicePaymentStatus.PAID,
        ).aggregate(
            total=Coalesce(Sum("total_amount"), zero, output_field=field),
            count=Count("id"),
        )

        # ── NEW: separate partial from unpaid ────────────────────────
        # Previously these were combined with .exclude(PAID).
        # Now each status gets its own query so the template can show them separately.
        partial_agg = base_qs.filter(
            payment_status=InvoicePaymentStatus.PARTIAL,
        ).aggregate(
            total=Coalesce(Sum("total_amount"), zero, output_field=field),
            count=Count("id"),
        )

        unpaid_agg = base_qs.filter(
            payment_status=InvoicePaymentStatus.UNPAID,
        ).aggregate(
            total=Coalesce(Sum("total_amount"), zero, output_field=field),
            count=Count("id"),
        )

        monthly = list(
            base_qs
            .annotate(month=TruncMonth("due_date"))
            .values("month")
            .annotate(total=Coalesce(Sum("total_amount"), zero, output_field=field))
            .order_by("month")
        )

        top_providers = list(
            base_qs
            .values("provider__name")
            .annotate(total=Coalesce(Sum("total_amount"), zero, output_field=field))
            .order_by("-total")[:10]
        )

        outstanding_qs = Invoice.objects.filter(
            company=company,
            due_date__lte=date_to,
        ).exclude(payment_status=InvoicePaymentStatus.PAID)

        outstanding_providers = list(
            outstanding_qs
            .values("provider__id", "provider__name")
            .annotate(
                total_owed=Coalesce(Sum("total_amount"), zero, output_field=field),
                invoice_count=Count("id"),
            )
            .order_by("-total_owed")[:15]
        )

        cumulative_purchase_debt = outstanding_qs.aggregate(
            total=Coalesce(Sum("total_amount"), zero, output_field=field),
            count=Count("id"),
        )

        return {
            "total": total_agg["total"],
            "count": total_agg["count"],
            "paid_total": paid_agg["total"],
            "paid_count": paid_agg["count"],
            # NEW
            "partial_total": partial_agg["total"],
            "partial_count": partial_agg["count"],
            "unpaid_total": unpaid_agg["total"],
            "unpaid_count": unpaid_agg["count"],
            "monthly": monthly,
            "top_providers": top_providers,
            "outstanding_providers": outstanding_providers,
            "cumulative_debt": cumulative_purchase_debt["total"],
            "cumulative_debt_count": cumulative_purchase_debt["count"],
        }

    @staticmethod
    def _compute_tax_periods(company, date_from, date_to):
        """
        Return TaxPeriod instances whose date range overlaps with the selected
        period, plus the immediately preceding period for each rule so the
        template can show carry-forward context.
        """
        if not company:
            return []

        from finance.models import TaxPeriod

        return list(
            TaxPeriod.objects.filter(
                company=company,
                date_to__gte=date_from,
                date_from__lte=date_to,
                rule__tax_type__behavior="add",
            )
            .select_related("rule")
            .order_by("date_from")
        )

    # ── Accounts Receivable aging helpers ──────────────────────────

    @staticmethod
    def _compute_ar_aging(company):
        """
        What clients owe us, bucketed by due date.
        Always rolling from TODAY — ignores the dashboard date range filter.
        Buckets: 0-30 days, 31-60 days, 61-90 days.
        """
        if not company:
            return {
                "bucket_30": Decimal("0.00"),
                "bucket_60": Decimal("0.00"),
                "bucket_90": Decimal("0.00"),
                "bucket_30_count": 0,
                "bucket_60_count": 0,
                "bucket_90_count": 0,
            }

        today = timezone.localdate()
        zero = Value(Decimal("0.00"))
        field = DecimalField(max_digits=12, decimal_places=2)

        base_qs = SalesInvoice.objects.filter(
            company=company,
            status=SalesInvoiceStatus.FINAL,
            due_date__isnull=False,
        ).exclude(payment_status=InvoicePaymentStatus.PAID)

        def bucket(date_from, date_to):
            qs = base_qs.filter(due_date__gte=date_from, due_date__lte=date_to)
            return qs.aggregate(
                total=Coalesce(Sum("total_amount"), zero, output_field=field),
                count=Count("id"),
            )

        b30 = bucket(today, today + timedelta(days=30))
        b60 = bucket(today + timedelta(days=31), today + timedelta(days=60))
        b90 = bucket(today + timedelta(days=61), today + timedelta(days=90))

        return {
            "bucket_30": b30["total"],
            "bucket_60": b60["total"],
            "bucket_90": b90["total"],
            "bucket_30_count": b30["count"],
            "bucket_60_count": b60["count"],
            "bucket_90_count": b90["count"],
        }

    # ── Accounts Payable aging helpers ─────────────────────────────

    @staticmethod
    def _compute_ap_aging(company):
        """
        What we owe providers, bucketed by due date.
        Always rolling from TODAY — ignores the dashboard date range filter.
        Buckets: 0-30 days, 31-60 days, 61-90 days.
        """
        if not company:
            return {
                "bucket_30": Decimal("0.00"),
                "bucket_60": Decimal("0.00"),
                "bucket_90": Decimal("0.00"),
                "bucket_30_count": 0,
                "bucket_60_count": 0,
                "bucket_90_count": 0,
            }

        today = timezone.localdate()
        zero = Value(Decimal("0.00"))
        field = DecimalField(max_digits=12, decimal_places=2)

        base_qs = Invoice.objects.filter(
            company=company,
            due_date__isnull=False,
        ).exclude(payment_status=InvoicePaymentStatus.PAID)

        def bucket(date_from, date_to):
            qs = base_qs.filter(due_date__gte=date_from, due_date__lte=date_to)
            return qs.aggregate(
                total=Coalesce(Sum("total_amount"), zero, output_field=field),
                count=Count("id"),
            )

        b30 = bucket(today, today + timedelta(days=30))
        b60 = bucket(today + timedelta(days=31), today + timedelta(days=60))
        b90 = bucket(today + timedelta(days=61), today + timedelta(days=90))

        return {
            "bucket_30": b30["total"],
            "bucket_60": b60["total"],
            "bucket_90": b90["total"],
            "bucket_30_count": b30["count"],
            "bucket_60_count": b60["count"],
            "bucket_90_count": b90["count"],
        }

    # ── Loan & credit helpers ─────────────────────────────────────

    @staticmethod
    def _compute_loan_data(company):
        """
        Loan debt summary for the selected company:
        - outstanding_balance: total outstanding across active loans
        - overdue: installments past due date, unpaid/partial
        - upcoming_30: installments due in next 30 days
        - upcoming_180: installments due in next 6 months (total financial debt horizon)
        - active_loans: per-loan breakdown with IBAN last 4 digits
        """
        if not company:
            return {
                "outstanding_balance": Decimal("0.00"),
                "overdue_total": Decimal("0.00"),
                "overdue_count": 0,
                "upcoming_30_total": Decimal("0.00"),
                "upcoming_30_count": 0,
                "upcoming_180_total": Decimal("0.00"),
                "upcoming_180_count": 0,
                "active_loans": [],
            }

        from loans.models import Loan, LoanInstallment, LoanInstallmentPaymentStatus, LoanStatus

        today = timezone.localdate()
        zero = Value(Decimal("0.00"))
        field = DecimalField(max_digits=14, decimal_places=2)

        active_loans = Loan.objects.filter(
            company=company,
            status=LoanStatus.ACTIVE,
        ).select_related("bank", "bank_account", "parent_loan")

        outstanding_balance = Decimal("0.00")
        active_loans_list = []
        for loan in active_loans:
            outstanding = loan.outstanding_principal
            amount = outstanding.amount if hasattr(outstanding, "amount") else Decimal(str(outstanding))
            outstanding_balance += amount

            # Last 4 digits of IBAN for identification
            iban = loan.bank_account.iban if loan.bank_account_id else ""
            iban_suffix = f"•••• {iban[-4:]}" if len(iban) >= 4 else ""

            active_loans_list.append({
                "name": loan.name or loan.reference or f"#{loan.pk}",
                "bank": str(loan.bank),
                "iban_suffix": iban_suffix,
                "outstanding": outstanding,
                "next_due": loan.installments.filter(
                    payment_status__in=["unpaid", "partial"]
                ).order_by("due_date").values_list("due_date", flat=True).first(),
            })

        # Sort by next due date ascending — most urgent first
        active_loans_list.sort(key=lambda x: x["next_due"] or date(9999, 12, 31))

        # Overdue installments
        overdue_qs = LoanInstallment.objects.filter(
            loan__company=company,
            loan__status=LoanStatus.ACTIVE,
            payment_status__in=[LoanInstallmentPaymentStatus.UNPAID, LoanInstallmentPaymentStatus.PARTIAL],
            due_date__lt=today,
        ).aggregate(
            total=Coalesce(Sum("total_due"), zero, output_field=field),
            count=Count("id"),
        )

        # Upcoming 0-30 days
        upcoming_30_qs = LoanInstallment.objects.filter(
            loan__company=company,
            loan__status=LoanStatus.ACTIVE,
            payment_status__in=[LoanInstallmentPaymentStatus.UNPAID, LoanInstallmentPaymentStatus.PARTIAL],
            due_date__gte=today,
            due_date__lte=today + timedelta(days=30),
        ).aggregate(
            total=Coalesce(Sum("total_due"), zero, output_field=field),
            count=Count("id"),
        )

        # Upcoming 6 months (0-180 days) — total financial debt horizon
        upcoming_180_qs = LoanInstallment.objects.filter(
            loan__company=company,
            loan__status=LoanStatus.ACTIVE,
            payment_status__in=[LoanInstallmentPaymentStatus.UNPAID, LoanInstallmentPaymentStatus.PARTIAL],
            due_date__gte=today,
            due_date__lte=today + timedelta(days=180),
        ).aggregate(
            total=Coalesce(Sum("total_due"), zero, output_field=field),
            count=Count("id"),
        )

        return {
            "outstanding_balance": outstanding_balance,
            "overdue_total": overdue_qs["total"],
            "overdue_count": overdue_qs["count"],
            "upcoming_30_total": upcoming_30_qs["total"],
            "upcoming_30_count": upcoming_30_qs["count"],
            "upcoming_180_total": upcoming_180_qs["total"],
            "upcoming_180_count": upcoming_180_qs["count"],
            "active_loans": active_loans_list,
        }

    # ── Bank balance helpers ───────────────────────────────────────

    @staticmethod
    def _compute_bank_balances(company):
        """Return linked bank accounts with their last known balance."""
        if not company:
            return []

        from company.models import BankAccount

        accounts = BankAccount.objects.filter(
            company=company,
        ).exclude(nuek_token="").order_by("bank_name")

        return [
            {
                "bank_name": a.bank_name,
                "iban": a.iban or a.nuek_product_id or "—",
                "balance": a.last_known_balance,
                "currency": a.last_known_balance_currency or "EUR",
                "updated_at": a.last_balance_updated_at,
            }
            for a in accounts
        ]