from django.shortcuts import get_object_or_404, render
from django.views.generic import TemplateView
from django.http import JsonResponse
from django.utils.translation import gettext as _
from unfold.views import UnfoldModelAdminViewMixin
from production.models import ProductionItem, ProductionItemStatus
from production.constants import ProductionActions
from company.models import Employee
import json

class ProductionItemKanbanView(UnfoldModelAdminViewMixin, TemplateView):
    title = _("Production Kanban")
    permission_required = ("production.view_productionitem",)
    template_name = "admin/production/productionitem/kanban.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        items = ProductionItem.objects.all().prefetch_related("assigned_employees", "ticket")
        employees = Employee.objects.filter(can_work_on_production=True, is_active=True)
        
        # Pre-process employees for template
        employee_data = []
        for emp in employees:
            employee_data.append({
                'pk': emp.pk,
                'initials': f"{emp.first_name[0]}{emp.last_name[0]}".upper() if emp.first_name and emp.last_name else "??",
                'full_name': str(emp),
            })

        kanban_columns = []
        for status, label in ProductionItemStatus.choices:
            column_items = []
            for item in items:
                if item.status == status:
                    # Pre-process assigned employees for this item
                    item_employees = []
                    for emp in item.assigned_employees.all():
                        item_employees.append({
                            'pk': emp.pk,
                            'initials': f"{emp.first_name[0]}{emp.last_name[0]}".upper() if emp.first_name and emp.last_name else "??",
                            'full_name': str(emp),
                        })

                    column_items.append({
                        'pk': item.pk,
                        'status_display': item.get_status_display(),
                        'component_display': item.data.get('component') or item.component or f"#{item.pk}",
                        'assigned_employees': item_employees,
                        'status_color': self._get_status_color(item.status)
                    })
            
            kanban_columns.append({
                "status": status,
                "label": label,
                "count": len(column_items),
                "items": column_items
            })
        
        context.update({
            "kanban_columns": kanban_columns,
            "employees": employee_data,
            "ProductionActions": ProductionActions,
        })
        return context

    def _get_status_color(self, status):
        if status == 'done': return 'green'
        if status == 'in_progress': return 'amber'
        return 'gray'

    def post(self, request, *args, **kwargs):
        if not request.user.has_perm("production.change_productionitem"):
            return JsonResponse({"error": _("Permission denied")}, status=403)

        try:
            data = json.loads(request.body)
            action = data.get("action")
            item_id = data.get("item_id")
            
            item = get_object_or_404(ProductionItem, pk=item_id)

            if action == ProductionActions.UPDATE_STATUS:
                new_status = data.get("status")
                if new_status in ProductionItemStatus.values:
                    item.status = new_status
                    item.save()
                    return JsonResponse({"success": True})
                return JsonResponse({"error": _("Invalid status")}, status=400)

            elif action == ProductionActions.ASSIGN_EMPLOYEE:
                employee_id = data.get("employee_id")
                employee = get_object_or_404(Employee, pk=employee_id)
                item.assigned_employees.add(employee)
                return JsonResponse({"success": True})

            elif action == ProductionActions.UNASSIGN_EMPLOYEE:
                employee_id = data.get("employee_id")
                employee = get_object_or_404(Employee, pk=employee_id)
                item.assigned_employees.remove(employee)
                return JsonResponse({"success": True})

            return JsonResponse({"error": _("Invalid action")}, status=400)

        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)
