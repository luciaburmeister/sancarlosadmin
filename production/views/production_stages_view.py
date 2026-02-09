from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.http import HttpRequest
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _  
from unfold.views import UnfoldModelAdminViewMixin

from production.models import ProductionItem, ProductionTicket
from production.services.stages_to_sessions import StagesToSessionsService
from production.constants import ProductionActions

class ProductionItemAdvanceStageView(UnfoldModelAdminViewMixin, View):
    permission_required = ("production.change_productionitem",)

    def post(self, request: HttpRequest, ticket_id: int, item_id: int):
        production_item = get_object_or_404(ProductionItem, pk=item_id, ticket_id=ticket_id)

        employee = getattr(request.user, "employee_profile", None)
        if employee is None:
            messages.error(request, _("Your user is not linked to an Employee."))
            return redirect(reverse("admin:production_productionticket_stages", args=[ticket_id]))

        action = request.POST.get("action")
        if action == ProductionActions.START_ACTIVITY:
            StagesToSessionsService.start_activity(production_item=production_item, employee=employee)

        elif action == ProductionActions.FINISH_ACTIVITY:
            StagesToSessionsService.finish_activity(production_item=production_item, employee=employee)

        elif action == ProductionActions.START_SESSION:
            StagesToSessionsService.start_session(production_item=production_item, employee=employee)
        
        elif action == ProductionActions.END_SESSION:
            StagesToSessionsService.end_session(production_item=production_item, employee=employee)

        return redirect(reverse("admin:production_productionticket_stages", args=[ticket_id]))