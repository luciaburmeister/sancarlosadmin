import io
from django.http import HttpResponse
from django.contrib.auth.mixins import UserPassesTestMixin
from django.views import View

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False


class TaxPeriodExportView(UserPassesTestMixin, View):
    """
    Export TaxPeriods to Excel.
    Recomputes all matching periods in chronological order before exporting
    so numbers always reflect final/approved invoices only and accumulated
    credit is correct.
    """

    def test_func(self):
        return self.request.user.is_staff

    def get(self, request):
        from .models import TaxPeriod, TaxScheduleRule

        if not HAS_OPENPYXL:
            return HttpResponse("openpyxl is required for Excel export.", status=500)

        qs = TaxPeriod.objects.select_related(
            "rule", "rule__tax_type", "company"
        ).order_by("rule__tax_type__tax_name", "date_from")

        company_id = request.GET.get("company")
        if company_id:
            qs = qs.filter(company_id=company_id)

        rule_id = request.GET.get("rule")
        if rule_id:
            qs = qs.filter(rule_id=rule_id)

        date_from = request.GET.get("date_from")
        date_to = request.GET.get("date_to")

        if date_from:
            qs = qs.filter(date_to__gte=date_from)
        if date_to:
            qs = qs.filter(date_from__lte=date_to)

        # Dashboard export: IVA only
        if request.GET.get("iva_only"):
            qs = qs.filter(rule__tax_type__tax_name__icontains="IVA")

        affected_rule_ids = qs.values_list("rule_id", flat=True).distinct()
        for rule in TaxScheduleRule.objects.filter(pk__in=affected_rule_ids):
            rule.compute_all_periods()

        qs = qs.all()

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Tax Periods"

        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill("solid", fgColor="0D9488")
        center = Alignment(horizontal="center", vertical="center")
        money_fmt = '#,##0.00 "€"'
        date_fmt = "DD/MM/YYYY"
        thin = Side(style="thin", color="E2E8F0")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        headers = [
            "Company", "Rule", "Tax Type",
            "Period Start", "Period End", "Due Date",
            "Tax Collected", "Tax Paid", "Tax Net",
            "Accumulated Credit", "Net to Pay",
            "Status",
        ]
        for col, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center
            cell.border = border

        ws.row_dimensions[1].height = 22

        for row_idx, period in enumerate(qs, 2):
            data = [
                str(period.company),
                period.rule.name,
                str(period.rule.tax_type),
                period.date_from,
                period.date_to,
                period.due_date,
                float(period.tax_collected.amount),
                float(period.tax_paid.amount),
                float(period.tax_net.amount),
                float(period.accumulated_credit.amount),
                float(period.net_after_credit.amount),
                period.get_status_display(),
            ]
            for col_idx, value in enumerate(data, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=value)
                cell.border = border
                cell.alignment = Alignment(vertical="center")

                if col_idx in (4, 5, 6):
                    cell.number_format = date_fmt
                if col_idx in (7, 8, 9, 10, 11):
                    cell.number_format = money_fmt
                if col_idx == 11:
                    cell.font = Font(
                        bold=True,
                        color="059669" if isinstance(value, float) and value >= 0 else "E11D48"
                    )
                if col_idx == 12:
                    status_colors = {
                        "Open": "3B82F6",
                        "Pending Payment": "F59E0B",
                        "Paid": "10B981",
                        "Overdue": "EF4444",
                    }
                    cell.font = Font(bold=True, color=status_colors.get(value, "64748B"))

        col_widths = [18, 28, 16, 14, 14, 14, 16, 16, 16, 18, 14, 14]
        for i, w in enumerate(col_widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

        ws.freeze_panes = "A2"

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        response = HttpResponse(
            buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = 'attachment; filename="tax_periods.xlsx"'
        return response