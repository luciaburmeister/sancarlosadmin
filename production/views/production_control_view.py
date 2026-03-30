from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from production.forms.production_control_filter_form import ProductionControlFilterForm
from production.models import ProductionItemSession, ProductionItemSessionStatus


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

    def get_queryset(self):
        qs = ProductionItemSession.objects.select_related(
            "company",
            "created_by",
            "production_item",
            "production_item__ticket",
            "production_item__component",
        ).filter(status=ProductionItemSessionStatus.IN_PROGRESS)

        form = ProductionControlFilterForm(self.request.GET or None)
        self.filter_form = form

        if form.is_valid():
            q = form.cleaned_data.get("q")
            company = form.cleaned_data.get("company")
            stage = form.cleaned_data.get("stage")

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

            if company:
                qs = qs.filter(company_id=company)

            if stage:
                qs = qs.filter(production_stage=stage)

        return qs.order_by("company__name", "production_stage", "start_time")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        sessions = self.get_queryset()
        ctx["filter_form"] = getattr(
            self,
            "filter_form",
            ProductionControlFilterForm(self.request.GET or None),
        )

        rows = []
        for s in sessions:
            employee = s.created_by
            company = s.company
            item = s.production_item
            ticket = item.ticket if item else None
            ticket_display = "—"
            if ticket:
                ticket_name = getattr(ticket.source_object, "name", None) or str(ticket.source_object)
                ticket_display = f"#{ticket.pk} - {ticket_name}"
            component = item.component if item else None

            component_name = str(component) if component else "—"
            item_summary = f"#{item.pk} - {component_name}" if item else "—"

            rows.append(
                {
                    "id": s.pk,
                    "employee_name": str(employee) if employee else "—",
                    "employee_url": reverse("admin:company_employee_change", args=[employee.pk]) if employee else "",
                    "company_name": str(company) if company else "—",
                    "stage": s.production_stage or "—",
                    "ticket_display": ticket_display,
                    "ticket_url": reverse("admin:production_productionticket_change", args=[ticket.pk]) if ticket else "",
                    "item_id": item.pk if item else None,
                    "item_url": reverse("admin:production_productionitem_change", args=[item.pk]) if item else "",
                    "item_summary": item_summary,
                    "component_name": component_name,
                    "started_at": s.start_time,
                    "started_at_display": s.start_time.strftime("%b %d, %Y, %H:%M") if s.start_time else "—",
                    "elapsed": self._format_elapsed(s.start_time),
                    "session_status": s.get_status_display(),
                    "session_url": reverse("admin:production_productionitemsession_change", args=[s.pk]),
                }
            )

        ctx["rows"] = rows
        ctx["active_count"] = len(rows)
        return ctx