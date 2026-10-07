import unicodedata

from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.http import HttpRequest
from django.contrib import messages
from django.utils.translation import gettext as _
from django.core.exceptions import ValidationError
from unfold.views import UnfoldModelAdminViewMixin
from django.core.exceptions import ValidationError
from company.models import Employee
from production.models import ProductionItem, ProductionItemSession, ProductionItemSessionStatus
from production.exceptions import StageHasActiveWorkersError, WorkerBusyError
from production.services.stages_to_sessions import StagesToSessionsService
from production.constants import ProductionActions
from django.utils.text import slugify
import logging
from django.utils import timezone
from production.models import ProductionItemSessionKind
logger = logging.getLogger(__name__)

class ProductionItemAdvanceStageView(UnfoldModelAdminViewMixin, View):
    permission_required = ("production.change_productionitem",)

    def _auto_leave_other_stages(self, employee, ticket, target_stage):
        """Close the employee's active sessions on OTHER stages of this ticket.

        This implements the 'switch activity' behavior: a worker can only
        be on one stage at a time, so starting a new stage automatically
        leaves any previous one.
        """
        active_sessions = ProductionItemSession.objects.filter(
            production_item__ticket=ticket,
            created_by=employee,
            status=ProductionItemSessionStatus.IN_PROGRESS,
            end_time__isnull=True,
        ).exclude(
            production_stage=target_stage,
        )

        
        now = timezone.now()
        count = 0
        for session in active_sessions:
            if session.kind == ProductionItemSessionKind.ACTIVITY_STARTED:
                session.kind = ProductionItemSessionKind.ACTIVITY_FINISHED
            elif session.kind == ProductionItemSessionKind.SESSION_STARTED:
                session.kind = ProductionItemSessionKind.SESSION_FINISHED
            session.finish(completed_by=employee)
            count += 1
        return count

    def post(self, request: HttpRequest, ticket_id: int, stage_name: str):
        ticket = get_object_or_404(self.model_admin.get_queryset(request), pk=ticket_id)
        action = request.POST.get("action")

        employee = getattr(request.user, "employee_profile", None)
        # Superusers may reopen stages without an employee profile.
        if employee is None and action != ProductionActions.REOPEN_STAGE:
            messages.error(request, _("Your user is not linked to an Employee."))
            return redirect(reverse("admin:production_productionticket_stages", args=[ticket_id]))

        restricted_worker = Employee.stage_restricted_worker(request.user)
        if restricted_worker is not None and (
            unicodedata.normalize("NFC", stage_name)
            not in restricted_worker.visible_production_stages(ticket.company_id)
        ):
            messages.error(request, _("You are not assigned to this production stage."))
            return redirect(reverse("admin:production_productionticket_stages", args=[ticket_id]))

        raw_ids = (request.POST.get("item_ids") or "").strip()
        anchor = request.POST.get("anchor")

        item_ids = [int(x) for x in raw_ids.split(",") if x.strip().isdigit()]
        if not item_ids:
            messages.error(request, _("No items selected for this stage action."))
            return redirect(reverse("admin:production_productionticket_stages", args=[ticket_id]))

        items = list(
            ProductionItem.objects.filter(ticket=ticket, id__in=item_ids).select_related("component", "company")
        )

        if action == ProductionActions.REOPEN_STAGE and not Employee.can_reopen_production_stages(
            request.user, ticket.company_id
        ):
            messages.error(request, _("Only production supervisors can reopen finished stages."))
            return redirect(reverse("admin:production_productionticket_stages", args=[ticket_id]))

        count = 0
        try:
            # Auto-leave other stages when starting/resuming (switch behavior)
            if action in (ProductionActions.START_ACTIVITY, ProductionActions.START_SESSION):
                left = self._auto_leave_other_stages(employee, ticket, stage_name)
                if left > 0:
                    logger.info(
                        "Auto-left %d sessions on other stages for employee %s switching to %s",
                        left, employee.pk, stage_name,
                    )

            if action == ProductionActions.START_ACTIVITY:
                count = StagesToSessionsService.start_activity(stage_name=stage_name, items=items, employee=employee)
                messages.success(request, _("Activity arrived to start successfully."))

            elif action == ProductionActions.FINISH_ACTIVITY:
                count = StagesToSessionsService.finish_activity(stage_name=stage_name, items=items, employee=employee)

            elif action == ProductionActions.START_SESSION:
                count = StagesToSessionsService.start_session(stage_name=stage_name, items=items, employee=employee)
            
            elif action == ProductionActions.END_SESSION:
                count = StagesToSessionsService.end_session(stage_name=stage_name, items=items, employee=employee)

            elif action == ProductionActions.UNDO_FINISH:
                count = StagesToSessionsService.undo_finish(stage_name=stage_name, items=items, employee=employee)

            elif action == ProductionActions.REOPEN_STAGE:
                count = StagesToSessionsService.revert_activity(
                    stage_name=stage_name,
                    items=items,
                    target_status=request.POST.get("target"),
                    user=request.user,
                    employee=employee,
                )

            if count > 0:
                messages.success(request, _("{} items processed successfully.").format(count))
            else:
                messages.warning(
                    request, 
                    _("No items were processed for stage '{}'. Check if they are already in the correct status.").format(stage_name)
                )
        except StageHasActiveWorkersError as e:
            names = ", ".join(w["full_name"] for w in e.active_workers)
            messages.error(
                request,
                _("Cannot finish: other workers are still active on this stage: %(names)s")
                % {"names": names},
            )
        except WorkerBusyError as e:
            messages.error(request, e.message)
        except ValidationError as e:
            logger.error(str(e))
            messages.error(request, _("Validation error: {}").format(" ".join(e.messages)))

        if action == ProductionActions.FINISH_ACTIVITY and items:
            # After finishing, try to find the next active stage to scroll there focus
            next_stage, __ = StagesToSessionsService._get_current_stage(items[0])
            if next_stage:
                anchor = f"stage-{slugify(next_stage)}"

        redirect_url = reverse("admin:production_productionticket_stages", args=[ticket_id])
        if anchor:
            redirect_url = f"{redirect_url}#{anchor}"

        return redirect(redirect_url)