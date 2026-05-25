"""Endpoints related to the authenticated user / employee profile."""

import unicodedata as _uc

from django.contrib.contenttypes.models import ContentType
from django.http import JsonResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from api._serializers import _ordered_stage_names, active_ticket_items
from api._utils import CompanyAPIView
from company.models import Employee
from configurator.models import Configuration, ConfigurationStatus
from management.models import ProjectFileStatus
from production.models import ProductionTicket, ProductionTicketStatus


def _line_assignee_ids(item) -> list[int]:
    return [e.id for e in item.assigned_employees.all()]


def _employee_can_work_stage(employee, stage_name_nfc: str) -> bool:
    allowed = [_uc.normalize("NFC", s) for s in (employee.production_stages or [])]
    if not allowed:
        return True
    return stage_name_nfc in allowed


def _task_pairs_capability_model(employee, active_items):
    pairs = []
    for ii, item in enumerate(active_items):
        data = item.data or {}
        component_label = data.get("component") or (
            str(item.component) if getattr(item, "component_id", None) else "—"
        )
        for jj, stage_name in enumerate(_ordered_stage_names(item)):
            if not _employee_can_work_stage(employee, stage_name):
                continue

            raw = {}
            for k, v in (item.planning or {}).items():
                if _uc.normalize("NFC", k) == stage_name:
                    raw = v if isinstance(v, dict) else {}
                    break
            info = raw if isinstance(raw, dict) else {}
            status = info.get("status") or "pending"
            ids = _line_assignee_ids(item)
            has_assignees = bool(ids)

            if status == "pending":
                if has_assignees and employee.id not in ids:
                    continue
            elif status == "in_progress":
                if has_assignees and employee.id not in ids:
                    continue
            elif status == "done":
                if employee.id not in ids:
                    continue

            pairs.append(
                {
                    "sort": (ii, jj),
                    "production_item_id": item.id,
                    "stage_name": stage_name,
                    "status": status,
                    "component_label": component_label,
                }
            )
    pairs.sort(key=lambda p: p["sort"])
    return pairs


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            employee = request.user.employee_profile
        except Employee.DoesNotExist:
            return JsonResponse({"error": "User has no employee profile linked."}, status=403)

        first_name = (employee.first_name or "").strip()
        last_name = (employee.last_name or "").strip()
        full_name = " ".join([p for p in [first_name, last_name] if p]).strip()

        return JsonResponse({
            "employee_id": employee.id,
            "employee_first_name": first_name or None,
            "employee_last_name": last_name or None,
            "employee_full_name": full_name or None,
            "username": request.user.username,
        })


class MyTasksView(CompanyAPIView):
    def get(self, request):
        employee = self.employee

        search = request.GET.get("search", "").strip()
        company = self.company

        ct = ContentType.objects.get_for_model(Configuration)
        active_config_ids = list(
            Configuration.objects.filter(
                company=company,
                status__in=(
                    ConfigurationStatus.CONVERTED,
                    ConfigurationStatus.SHOP,
                ),
            ).values_list("id", flat=True)
        )

        tickets = (
            ProductionTicket.objects.filter(
                company=company,
                content_type=ct,
                object_id__in=active_config_ids,
            )
            .select_related("project_file__client")
            .prefetch_related("items__assigned_employees", "items__component")
        )

        results = []
        for ticket in tickets:
            if ticket.status == ProductionTicketStatus.CANCELLED:
                continue

            config = ticket.source_object
            if config is None:
                continue

            active_items = active_ticket_items(ticket)

            project_file = ticket.project_file
            if project_file is None:
                try:
                    project_file = config.quote.project_file
                except Exception:
                    project_file = None

            if project_file is not None and project_file.status == ProjectFileStatus.CANCELLED:
                continue

            if search and project_file:
                name_match = search.lower() in (project_file.name or "").lower()
                client_name = project_file.client.name if project_file.client else ""
                client_match = search.lower() in client_name.lower()
                if not name_match and not client_match:
                    continue

            my_pairs = _task_pairs_capability_model(employee, active_items)
            if not my_pairs:
                continue

            my_stages_total = len(my_pairs)
            my_stages_done = sum(1 for p in my_pairs if p["status"] == "done")

            if my_stages_done == my_stages_total:
                continue

            next_p = next(
                (p for p in my_pairs if p["status"] != "done"),
                my_pairs[0],
            )

            expediente_data = None
            if project_file:
                start_date = project_file.start_date
                expediente_data = {
                    "id": project_file.id,
                    "reference": getattr(project_file, "reference", None) or "",
                    "name": project_file.name or "",
                    "client_name": project_file.client.name if project_file.client else None,
                    "start_date": (
                        start_date.strftime("%Y-%m-%d")
                        if hasattr(start_date, "strftime")
                        else start_date
                    ),
                }

            quote = getattr(config, "quote", None)
            quote_name = (
                (quote.project_name or "").strip()
                if quote and (quote.project_name or "").strip()
                else (getattr(config, "name", None) or "")
            )

            results.append({
                "config_id": config.id,
                "config_name": getattr(config, "name", None) or "",
                "quote_name": quote_name,
                "template_name": config.product.name if getattr(config, "product", None) else None,
                "ticket_status": ticket.status,
                "expediente": expediente_data,
                "next_stage": {
                    "name": next_p["stage_name"],
                    "status": next_p["status"],
                    "production_item_id": next_p["production_item_id"],
                    "component_label": next_p["component_label"],
                },
                "my_stages_total": my_stages_total,
                "my_stages_done": my_stages_done,
            })

        return JsonResponse({"results": results, "count": len(results)})
