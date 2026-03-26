from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.http import HttpRequest
from django.contrib import messages
from django.utils.translation import gettext as _
from django.utils.text import slugify
from unfold.views import UnfoldModelAdminViewMixin

from production.models import ProductionTicket, ProductionItem, ProductionItemStatus
from production.services.stages_to_sessions import StagesToSessionsService
from production.constants import ProductionActions


class ProductionItemAdvanceStageView(UnfoldModelAdminViewMixin, View):
    permission_required = ("production.change_productionitem",)

    def post(self, request: HttpRequest, ticket_id: int, stage_name: str):
        ticket = get_object_or_404(ProductionTicket, pk=ticket_id)

        employee = getattr(request.user, "employee_profile", None)
        if employee is None:
            messages.error(request, _("Your user is not linked to an Employee."))
            return redirect(reverse("admin:production_productionticket_stages", args=[ticket_id]))

        action = request.POST.get("action")
        raw_ids = (request.POST.get("item_ids") or "").strip()
        anchor = request.POST.get("anchor")

        item_ids = [int(x) for x in raw_ids.split(",") if x.strip().isdigit()]
        if not item_ids:
            messages.error(request, _("No items selected for this stage action."))
            return redirect(reverse("admin:production_productionticket_stages", args=[ticket_id]))

        items = list(
            ProductionItem.objects.filter(ticket=ticket, id__in=item_ids).select_related("component")
        )

        if action == ProductionActions.START_ACTIVITY:
            StagesToSessionsService.start_activity(stage_name=stage_name, items=items, employee=employee)
        
        elif action == ProductionActions.FINISH_ACTIVITY:
            StagesToSessionsService.finish_activity(stage_name=stage_name, items=items, employee=employee)

        elif action == ProductionActions.START_SESSION:
            StagesToSessionsService.start_session(stage_name=stage_name, items=items, employee=employee)
        
        elif action == ProductionActions.END_SESSION:
            StagesToSessionsService.end_session(stage_name=stage_name, items=items, employee=employee)

        if action == ProductionActions.FINISH_ACTIVITY and items:
            # After finishing, try to find the next active stage to scroll there focus
            next_stage, _ = StagesToSessionsService._get_current_stage(items[0])
            if next_stage:
                anchor = f"stage-{slugify(next_stage)}"

        redirect_url = reverse("admin:production_productionticket_stages", args=[ticket_id])
        if anchor:
            redirect_url = f"{redirect_url}#{anchor}"

        return redirect(redirect_url)