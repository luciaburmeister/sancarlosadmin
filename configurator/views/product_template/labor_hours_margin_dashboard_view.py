from datetime import datetime
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.paginator import Paginator
from django.db.models import DurationField, ExpressionWrapper, F, Sum
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from company.models import Employee
from core.admin.company_access import get_user_companies
from configurator.forms.labor_hours_margin_dashboard_filters import LaborHoursMarginDashboardFilterForm
from configurator.models import Configuration, ConfigurationStatus
from management.models import ProjectFile
from production.models import ProductionItemSession, ProductionTicket

_BILLABLE_STATUSES = [
    ConfigurationStatus.SHOP,
    ConfigurationStatus.COMPLETED,
    ConfigurationStatus.CONVERTED,
    ConfigurationStatus.PRODUCTION_DONE,
]

# Deviation can't exceed +100% (real_hours can't go below 0), but has no floor
# on the negative side (real_hours can vastly exceed billed_hours). Past this
# magnitude a row is flagged for manual review rather than silently averaged
# into the aggregate KPI -- it likely means a placeholder/under-quantified
# billed line, or a session that was never properly closed.
_OUTLIER_DEVIATION_THRESHOLD = Decimal("300")

# Chart bar count -- raised from an earlier fixed 15. The canvas height scales
# with this in the template's JS, so bars stay thick enough to hover reliably
# instead of the tooltip flickering between razor-thin adjacent bars.
_CHART_MAX_ROWS = 30

# Only used when there's no company to read Company.ideal_daily_work_minutes
# from (e.g. a user with no companies at all) -- not the normal path.
_FALLBACK_IDEAL_DAILY_MINUTES = 480


class LaborHoursMarginDashboardView(UnfoldModelAdminViewMixin, TemplateView):
    """Billed labor hours vs. real hours worked, per expediente, company-wide."""

    title = _("Labor Hours Margin Dashboard")
    permission_required = ("configurator.labor_hours_margin_dashboard",)
    template_name = "admin/configurator/producttemplate/labor_hours_margin_dashboard.html"
    model_admin = None

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request

        user_companies = get_user_companies(request.user)
        company = self._resolve_company(request, user_companies)
        filter_form = LaborHoursMarginDashboardFilterForm(request.GET)
        date_from, date_to = self._resolve_dates(filter_form)

        sort_order = self._resolve_sort_order(request)
        search_query = request.GET.get("q", "").strip()
        selected_day = self._resolve_day(request)
        ideal_daily_minutes = company.ideal_daily_work_minutes if company else _FALLBACK_IDEAL_DAILY_MINUTES
        mechanic_rows = self._compute_mechanic_rows(company, selected_day, ideal_daily_minutes) if company else []

        rows = self._compute_rows(company, date_from, date_to) if company else []
        if search_query:
            rows = self._filter_rows(rows, search_query)

        sort_key, sort_reverse = self._sort_key_and_reverse(sort_order)
        rows.sort(key=sort_key, reverse=sort_reverse)

        # Chart follows the same sort as the table (per user choice), just
        # capped to the first N rows so the bars stay readable.
        chart_rows = rows[:_CHART_MAX_ROWS]
        chart_data = {
            "labels": [
                (r["project_file"].reference or f"#{r['project_file'].pk}")
                for r in chart_rows
            ],
            "real_hours": [float(r["real_hours"]) for r in chart_rows],
            "gap_hours": [float(r["gap_hours"]) for r in chart_rows],
            "urls": [
                reverse("admin:management_projectfile_change", args=[r["project_file"].pk])
                for r in chart_rows
            ],
        }

        paginator = Paginator(rows, 20)
        page = paginator.get_page(request.GET.get("page"))

        zero = Decimal("0")
        total_billed_hours = sum((r["billed_hours"] for r in rows), zero)
        total_gap_hours = sum((r["gap_hours"] for r in rows), zero)
        totals = {
            "billed_hours": total_billed_hours,
            "real_hours": sum((r["real_hours"] for r in rows), zero),
            "gap_hours": total_gap_hours,
            "extra_margin": sum((r["extra_margin"] for r in rows), zero),
            "expediente_count": len(rows),
            # Weighted by hours (total gap / total billed), not an average of
            # each row's own %, so a handful of small expedientes with extreme
            # deviations can't skew the headline number.
            "deviation_pct": (total_gap_hours / total_billed_hours * 100) if total_billed_hours else zero,
            "outlier_count": sum(1 for r in rows if r["is_deviation_outlier"]),
        }

        mechanic_totals = {
            "mechanic_count": len(mechanic_rows),
            "below_ideal_count": sum(1 for r in mechanic_rows if r["worked_minutes"] < ideal_daily_minutes),
            "avg_worked_hours": (
                sum((r["worked_hours"] for r in mechanic_rows), zero) / len(mechanic_rows)
                if mechanic_rows else zero
            ),
        }

        ctx.update({
            "companies": user_companies,
            "selected_company": company,
            "filter_form": filter_form,
            "date_from": date_from.isoformat() if date_from else "",
            "date_to": date_to.isoformat() if date_to else "",
            "sort_order": sort_order,
            "search_query": search_query,
            "page": page,
            "totals": totals,
            "chart_data": chart_data,
            "chart_has_data": bool(chart_rows),
            "selected_day": selected_day,
            "mechanic_rows": mechanic_rows,
            "mechanic_totals": mechanic_totals,
            "ideal_daily_hours": Decimal(ideal_daily_minutes) / Decimal(60),
        })
        return ctx

    @staticmethod
    def _filter_rows(rows, search_query):
        """Case-insensitive match on expediente reference, name, or client."""
        needle = search_query.lower()
        matches = []
        for row in rows:
            pf = row["project_file"]
            haystack = " ".join(filter(None, [pf.reference, pf.name, str(pf.client)])).lower()
            if needle in haystack:
                matches.append(row)
        return matches

    @staticmethod
    def _resolve_sort_order(request):
        value = request.GET.get("sort")
        return value if value in ("asc", "desc", "date_desc", "date_asc") else "desc"

    @staticmethod
    def _sort_key_and_reverse(sort_order):
        if sort_order == "date_desc":
            return (lambda r: r["project_file"].created_at), True
        if sort_order == "date_asc":
            return (lambda r: r["project_file"].created_at), False
        # "asc" / "desc" -- by margin (gap_hours), the original behavior.
        return (lambda r: r["gap_hours"]), (sort_order == "desc")

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
        filter_form.is_valid()
        cleaned = filter_form.cleaned_data
        return cleaned.get("date_from"), cleaned.get("date_to")

    @staticmethod
    def _resolve_day(request):
        value = request.GET.get("day")
        if value:
            try:
                return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                pass
        return timezone.localdate()

    @staticmethod
    def _compute_mechanic_rows(company, day, ideal_daily_minutes):
        """Every mechanic who can work on production, with their worked time
        on `day` vs. the company's ideal daily minutes. Mechanics with zero
        sessions that day are included (worked_minutes=0), so absences are
        visible too. The comparison itself is done in minutes (matching the
        storage unit, more flexible than decimal hours -- e.g. 450 for
        7h30m); hours are derived only for display.
        """
        mechanics = Employee.objects.filter(
            company=company, can_work_on_production=True, is_active=True,
        ).order_by("last_name", "first_name")

        seconds_by_employee = dict(
            ProductionItemSession.objects.completed()
            .filter(company=company, start_time__date=day, created_by__isnull=False)
            .values("created_by_id")
            .annotate(total=Sum(
                ExpressionWrapper(F("end_time") - F("start_time"), output_field=DurationField())
            ))
            .values_list("created_by_id", "total")
        )

        rows = []
        for mechanic in mechanics:
            total_seconds = seconds_by_employee.get(mechanic.id)
            worked_minutes = Decimal(total_seconds.total_seconds()) / Decimal(60) if total_seconds else Decimal("0")
            deviation_pct = ((worked_minutes - ideal_daily_minutes) / ideal_daily_minutes) * 100
            rows.append({
                "mechanic": mechanic,
                "worked_minutes": worked_minutes,
                "ideal_minutes": ideal_daily_minutes,
                "worked_hours": worked_minutes / Decimal(60),
                "ideal_hours": Decimal(ideal_daily_minutes) / Decimal(60),
                "deviation_pct": deviation_pct,
            })
        rows.sort(key=lambda r: r["worked_minutes"])
        return rows

    @staticmethod
    def _compute_rows(company, date_from, date_to):
        """One row per expediente with at least one labor-flagged Configuration.

        Real hours are resolved via BOTH the direct ProductionTicket.project_file
        FK and, for tickets that predate it, the Configuration's quote/direct
        project_file -- but only for the (small) set of expedientes that already
        have billed labor hours, so the fallback resolution stays cheap
        regardless of company-wide ticket volume.
        """
        billed_qs = Configuration.objects.filter(
            company=company,
            product__is_labor_hours_template=True,
            status__in=_BILLABLE_STATUSES,
        )
        if date_from:
            billed_qs = billed_qs.filter(created_at__date__gte=date_from)
        if date_to:
            billed_qs = billed_qs.filter(created_at__date__lte=date_to)

        billed_qs = (
            billed_qs
            .annotate(resolved_project_file_id=Coalesce("quote__project_file_id", "project_file_id"))
            .exclude(resolved_project_file_id__isnull=True)
            .values("resolved_project_file_id")
            .annotate(billed_hours=Sum("units"), billed_amount=Sum("subtotal_price"))
        )

        billed_by_pf = {}
        for row in billed_qs:
            billed_hours = row["billed_hours"] or Decimal("0")
            if billed_hours <= 0:
                # A labor-flagged template with no unit_price_variable (or one
                # whose divisor evaluated to 0) yields Configuration.units == 0
                # -- there's no real billed-hours figure to compare, so skip it
                # rather than showing a meaningless 0.00 row.
                continue
            billed_amount = row["billed_amount"] or Decimal("0")
            if hasattr(billed_amount, "amount"):
                billed_amount = billed_amount.amount
            billed_by_pf[row["resolved_project_file_id"]] = {
                "billed_hours": billed_hours,
                "billed_amount": billed_amount,
            }

        pf_ids = set(billed_by_pf.keys())
        if not pf_ids:
            return []

        real_seconds_by_pf = LaborHoursMarginDashboardView._compute_real_seconds(company, pf_ids)

        project_files = {
            pf.id: pf
            for pf in ProjectFile.objects.filter(id__in=pf_ids).select_related("client")
        }

        rows = []
        for pf_id, billed in billed_by_pf.items():
            project_file = project_files.get(pf_id)
            if not project_file:
                continue
            billed_hours = billed["billed_hours"]
            billed_amount = billed["billed_amount"]
            real_hours = Decimal(real_seconds_by_pf.get(pf_id, 0)) / Decimal(3600)
            gap_hours = billed_hours - real_hours
            hourly_rate = (billed_amount / billed_hours) if billed_hours else Decimal("0")
            extra_margin = gap_hours * hourly_rate
            # billed_hours is always > 0 here (rows with billed_hours <= 0 were
            # already skipped above), so this division is always safe.
            deviation_pct = (gap_hours / billed_hours) * 100
            rows.append({
                "project_file": project_file,
                "billed_hours": billed_hours,
                "real_hours": real_hours,
                "gap_hours": gap_hours,
                "extra_margin": extra_margin,
                "deviation_pct": deviation_pct,
                "is_deviation_outlier": abs(deviation_pct) > _OUTLIER_DEVIATION_THRESHOLD,
            })
        return rows

    @staticmethod
    def _compute_real_seconds(company, pf_ids):
        """Total completed-session seconds per project_file id, resolved via the
        direct ProductionTicket.project_file FK plus the quote/direct fallback
        for tickets that predate it -- bounded to the given pf_ids.
        """
        real_seconds_by_pf = {}

        direct_rows = (
            ProductionItemSession.objects.completed()
            .filter(company=company, production_item__ticket__project_file_id__in=pf_ids)
            .values("production_item__ticket__project_file_id")
            .annotate(total=Sum(
                ExpressionWrapper(F("end_time") - F("start_time"), output_field=DurationField())
            ))
        )
        for row in direct_rows:
            real_seconds_by_pf[row["production_item__ticket__project_file_id"]] = int(row["total"].total_seconds())

        config_ct = ContentType.objects.get_for_model(Configuration)
        null_tickets = list(
            ProductionTicket.objects.filter(
                company=company, project_file__isnull=True, content_type=config_ct,
            ).values_list("id", "object_id")
        )
        object_ids = [oid for _, oid in null_tickets]
        if object_ids:
            config_pf_map = dict(
                Configuration.objects.filter(id__in=object_ids)
                .annotate(resolved_project_file_id=Coalesce("quote__project_file_id", "project_file_id"))
                .values_list("id", "resolved_project_file_id")
            )
            ticket_to_pf = {
                ticket_id: config_pf_map[obj_id]
                for ticket_id, obj_id in null_tickets
                if config_pf_map.get(obj_id) in pf_ids
            }
            if ticket_to_pf:
                fallback_rows = (
                    ProductionItemSession.objects.completed()
                    .filter(production_item__ticket_id__in=ticket_to_pf.keys())
                    .values("production_item__ticket_id")
                    .annotate(total=Sum(
                        ExpressionWrapper(F("end_time") - F("start_time"), output_field=DurationField())
                    ))
                )
                for row in fallback_rows:
                    pf_id = ticket_to_pf[row["production_item__ticket_id"]]
                    real_seconds_by_pf[pf_id] = real_seconds_by_pf.get(pf_id, 0) + int(row["total"].total_seconds())

        return real_seconds_by_pf
