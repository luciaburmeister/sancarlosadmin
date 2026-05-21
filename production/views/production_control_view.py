from collections import OrderedDict

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from production.forms.production_control_filter_form import ProductionControlFilterForm
from production.models import ProductionItemSession, ProductionItemSessionStatus
from company.models import Employee


class ProductionControlView(UnfoldModelAdminViewMixin, TemplateView):
    title = "Production control"
    permission_required = ("production.view_productionitemsession",)
    template_name = "admin/production/production_control.html"

    def _format_elapsed(self, start_time):
        if not start_time:
            return "—"

        delta = timezone.now() - start_time
        total_seconds = max(int(delta.total_seconds()), 0)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, _seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}"

    def _get_first_unfinished_stage(self, ticket):
        if not ticket:
            return "—"

        completed_statuses = {
            getattr(ProductionItemSessionStatus, "DONE", None),
            getattr(ProductionItemSessionStatus, "COMPLETED", None),
            getattr(ProductionItemSessionStatus, "FINISHED", None),
        }
        completed_statuses.discard(None)

        sessions = (
            ProductionItemSession.objects.filter(
                production_item__ticket=ticket,
            )
            .order_by("start_time", "pk")
        )

        for session in sessions:
            if session.status not in completed_statuses:
                return session.production_stage or "—"

        return "Completed"

    def get_queryset(self):
        qs = ProductionItemSession.objects.select_related(
            "company",
            "created_by",
            "production_item",
            "production_item__ticket",
            "production_item__component",
            "production_item__ticket__content_type",
        ).filter(status=ProductionItemSessionStatus.IN_PROGRESS)

        user = self.request.user

        if not user.is_superuser:
            employee = (
                Employee.objects.filter(
                    user=user,
                    is_active=True,
                    company__isnull=False,
                )
                .select_related("company")
                .first()
            )

            if employee and employee.company:
                qs = qs.filter(company=employee.company)
            else:
                qs = qs.none()

        form = ProductionControlFilterForm(self.request.GET or None)
        self.filter_form = form

        if form.is_valid():
            q = form.cleaned_data.get("q")
            stage = form.cleaned_data.get("stage")
            company = form.cleaned_data.get("company")

            if q:
                qs = qs.filter(
                    Q(created_by__first_name__icontains=q)
                    | Q(created_by__last_name__icontains=q)
                    | Q(created_by__email__icontains=q)
                    | Q(production_stage__icontains=q)
                    | Q(production_item__component__name__icontains=q)
                    | Q(production_item__ticket__id__icontains=q)
                    | Q(production_item__id__icontains=q)
                )

            if stage:
                qs = qs.filter(production_stage=stage)

            if company:
                qs = qs.filter(company=company)

        return qs.order_by(
            "company__name",
            "created_by__first_name",
            "created_by__last_name",
            "production_item__ticket_id",
            "production_stage",
            "start_time",
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        sessions = self.get_queryset()
        ctx["filter_form"] = getattr(
            self,
            "filter_form",
            ProductionControlFilterForm(self.request.GET or None),
        )

        grouped = OrderedDict()

        for s in sessions:
            employee = s.created_by
            company = s.company
            item = s.production_item
            ticket = item.ticket if item else None
            stage = s.production_stage or "—"

            key = (
                employee.pk if employee else None,
                ticket.pk if ticket else None,
                stage,
            )

            if key not in grouped:
                ticket_display = "—"
                ticket_url = ""
                if ticket:
                    ticket_name = getattr(ticket.source_object, "name", None) or str(ticket.source_object)
                    ticket_display = f"#{ticket.pk} - {ticket_name}"
                    ticket_url = reverse("admin:production_productionticket_change", args=[ticket.pk])

                grouped[key] = {
                    "employee_name": str(employee) if employee else "—",
                    "employee_url": reverse("admin:company_employee_change", args=[employee.pk]) if employee else "",
                    "company_name": str(company) if company else "—",
                    "stage": stage,
                    "ticket_display": ticket_display,
                    "ticket_url": ticket_url,
                    "first_unfinished_stage": self._get_first_unfinished_stage(ticket),
                    "items_count": 0,
                    "items": [],
                    "started_at": s.start_time,
                    "started_at_display": s.start_time.strftime("%b %d, %Y, %H:%M") if s.start_time else "—",
                    "elapsed": self._format_elapsed(s.start_time),
                    "session_status": s.get_status_display(),
                    "session_ids": [],
                }

            row = grouped[key]

            if s.start_time and (row["started_at"] is None or s.start_time < row["started_at"]):
                row["started_at"] = s.start_time
                row["started_at_display"] = s.start_time.strftime("%b %d, %Y, %H:%M")

            row["session_ids"].append(s.pk)

            if item:
                component = item.component
                component_name = str(component) if component else "—"

                row["items"].append(
                    {
                        "id": item.pk,
                        "url": reverse("admin:production_productionitem_change", args=[item.pk]),
                        "summary": f"#{item.pk} - {component_name}",
                        "component_name": component_name,
                        "session_url": reverse("admin:production_productionitemsession_change", args=[s.pk]),
                    }
                )
                row["items_count"] += 1

        rows = list(grouped.values())

        for row in rows:
            row["elapsed"] = self._format_elapsed(row["started_at"])

        ctx["rows"] = rows
        ctx["active_count"] = len(rows)
        return ctx