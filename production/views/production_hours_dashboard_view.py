from django.db.models import Count, DurationField, ExpressionWrapper, F, Sum
from django.db.models.functions import TruncDate, TruncMonth, TruncWeek, TruncYear
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from company.models import Employee
from core.admin.company_access import get_user_companies
from production.forms.hours_dashboard_filters import ProductionHoursDashboardFilterForm
from production.models import HoursGranularity, ProductionItemSession

_TRUNC_FUNCTIONS = {
    HoursGranularity.DAY: TruncDate,
    HoursGranularity.WEEK: TruncWeek,
    HoursGranularity.MONTH: TruncMonth,
    HoursGranularity.YEAR: TruncYear,
}


class ProductionHoursDashboardView(UnfoldModelAdminViewMixin, TemplateView):
    """Worked hours per employee, aggregated by day/week/month/year."""

    title = _("Production Hours Dashboard")
    permission_required = ("production.production_hours_dashboard",)
    template_name = "admin/production/productionticket/hours_dashboard.html"
    model_admin = None

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request

        user_companies = get_user_companies(request.user)
        company = self._resolve_company(request, user_companies)
        filter_form = ProductionHoursDashboardFilterForm(request.GET)
        date_from, date_to, day = self._resolve_dates(filter_form)
        granularity = self._resolve_granularity(request)
        employee = self._resolve_employee(request, company)

        leaderboard = self._compute_leaderboard(company, date_from, date_to) if company else []
        buckets = (
            self._compute_buckets(
                company, date_from, date_to, granularity,
                employee_id=employee.id if employee else None,
            )
            if company else []
        )
        day_sessions = (
            self._compute_day_sessions(company, day, employee_id=employee.id if employee else None)
            if company and day else []
        )

        total_seconds = sum(row["total_seconds"] for row in leaderboard)
        employee_count = len(leaderboard)

        ctx.update({
            "companies": user_companies,
            "selected_company": company,
            "filter_form": filter_form,
            "date_from": date_from.isoformat() if date_from else "",
            "date_to": date_to.isoformat() if date_to else "",
            "granularity": granularity,
            "granularity_choices": HoursGranularity.choices,
            "day_granularity": HoursGranularity.DAY,
            "employees": (
                Employee.objects.filter(
                    company=company, can_work_on_production=True, is_active=True,
                ).order_by("last_name", "first_name")
                if company else Employee.objects.none()
            ),
            "selected_employee": employee,
            "leaderboard": leaderboard,
            "buckets": buckets,
            "selected_day": day.isoformat() if day else "",
            "day_sessions": day_sessions,
            "total_hours": round(total_seconds / 3600, 2),
            "session_count": sum(row["sessions"] for row in leaderboard),
            "employee_count": employee_count,
            "avg_hours_per_employee": (
                round(total_seconds / 3600 / employee_count, 2) if employee_count else 0
            ),
        })
        return ctx

    @staticmethod
    def _resolve_company(request, user_companies):
        selected_company_id = request.GET.get("company")
        try:
            selected_company_id = int(selected_company_id) if selected_company_id else None
        except (TypeError, ValueError):
            selected_company_id = None

        if selected_company_id and user_companies.filter(id=selected_company_id).exists():
            return user_companies.get(id=selected_company_id)
        return user_companies.first()

    @staticmethod
    def _resolve_dates(filter_form):
        """Unbounded by default so the dashboard shows full history; narrow via GET params.

        Each field is read independently from cleaned_data so one invalid field
        (e.g. a hand-typed date) doesn't discard the other, valid ones.
        """
        filter_form.is_valid()
        cleaned = filter_form.cleaned_data
        return cleaned.get("date_from"), cleaned.get("date_to"), cleaned.get("day")

    @staticmethod
    def _resolve_granularity(request):
        value = request.GET.get("granularity")
        return value if value in HoursGranularity.values else HoursGranularity.MONTH

    @staticmethod
    def _resolve_employee(request, company):
        employee_id = request.GET.get("employee")
        if not company or not employee_id:
            return None
        return Employee.objects.filter(pk=employee_id, company=company).first()

    @staticmethod
    def _compute_leaderboard(company, date_from, date_to):
        """Per-employee worked-hours totals for the period (or all time), most-worked first."""
        qs = ProductionItemSession.objects.completed().filter(
            company=company, created_by__isnull=False,
        )
        if date_from:
            qs = qs.filter(start_time__date__gte=date_from)
        if date_to:
            qs = qs.filter(start_time__date__lte=date_to)

        rows = (
            qs
            .annotate(
                duration=ExpressionWrapper(F("end_time") - F("start_time"), output_field=DurationField()),
            )
            .values("created_by_id", "created_by__first_name", "created_by__last_name")
            .annotate(total=Sum("duration"), sessions=Count("id"))
            .order_by("-total")
        )
        entries = []
        for row in rows:
            total_seconds = int(row["total"].total_seconds())
            hours, remainder = divmod(total_seconds, 3600)
            entries.append({
                "employee_id": row["created_by_id"],
                "employee_name": f"{row['created_by__first_name']} {row['created_by__last_name']}".strip(),
                "total_seconds": total_seconds,
                "total_hours_label": f"{hours}h {remainder // 60}m",
                "sessions": row["sessions"],
            })
        return entries

    @staticmethod
    def _compute_buckets(company, date_from, date_to, granularity, employee_id=None):
        """Worked-hours totals bucketed by day/week/month/year, over the range (or all time)."""
        trunc = _TRUNC_FUNCTIONS[granularity]
        qs = ProductionItemSession.objects.completed().filter(company=company)
        if date_from:
            qs = qs.filter(start_time__date__gte=date_from)
        if date_to:
            qs = qs.filter(start_time__date__lte=date_to)
        if employee_id:
            qs = qs.filter(created_by_id=employee_id)

        rows = (
            qs.annotate(
                bucket=trunc("start_time"),
                duration=ExpressionWrapper(F("end_time") - F("start_time"), output_field=DurationField()),
            )
            .values("bucket")
            .annotate(total=Sum("duration"))
            .order_by("bucket")
        )
        return [
            {"period_start": row["bucket"], "total_seconds": int(row["total"].total_seconds())}
            for row in rows
            if row["bucket"] is not None
        ]

    @staticmethod
    def _compute_day_sessions(company, day, employee_id=None):
        """Individual sessions for one day, with the expediente each belonged to."""
        qs = (
            ProductionItemSession.objects.completed()
            .filter(company=company, start_time__date=day)
            .select_related("created_by", "production_item__ticket__project_file")
            .order_by("start_time")
        )
        if employee_id:
            qs = qs.filter(created_by_id=employee_id)

        sessions = []
        for session in qs:
            project_file = session.production_item.ticket.resolved_project_file
            sessions.append({
                "employee_name": session.created_by.full_name if session.created_by else "—",
                "stage": session.production_stage,
                "start_time": session.start_time,
                "end_time": session.end_time,
                "duration_label": session.formatted_duration,
                "project_file": project_file,
            })
        return sessions
