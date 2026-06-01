import logging

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views import View

from unfold.views import UnfoldModelAdminViewMixin

from company.models import Employee
from production.constants import ProductionActions
from production.models import AssemblyLine
from production.services.assembly_line_service import AssemblyLineService

logger = logging.getLogger(__name__)


class AssemblyLineActionView(UnfoldModelAdminViewMixin, View):
    """Handles assembly line start/pause/finish actions via POST.

    Designed to be called from any page (ticket stages, operate view, etc.)
    and redirects back to the referrer.
    """

    permission_required = ("production.change_assemblyline",)

    def _get_employee(self, request):
        employee = getattr(request.user, "employee_profile", None)
        if employee is None:
            employee = (
                Employee.objects.filter(user=request.user, is_active=True)
                .first()
            )
        return employee

    def post(self, request, assembly_line_id):
        assembly_line = get_object_or_404(AssemblyLine, pk=assembly_line_id)
        employee = self._get_employee(request)

        redirect_url = request.POST.get("redirect_url") or request.META.get(
            "HTTP_REFERER",
            reverse("admin:production_assemblyline_changelist"),
        )

        if employee is None:
            messages.error(request, _("Your user is not linked to an Employee."))
            return redirect(redirect_url)

        action = request.POST.get("action")

        try:
            if action == ProductionActions.START_LINE:
                result = AssemblyLineService.start_line(assembly_line, employee)
                count = result["started"]
                if count > 0:
                    messages.success(
                        request,
                        _("Started %(count)d items across all tickets.")
                        % {"count": count},
                    )
                else:
                    messages.warning(
                        request,
                        _("No items were eligible to start."),
                    )

            elif action == ProductionActions.PAUSE_LINE:
                result = AssemblyLineService.pause_line(assembly_line, employee)
                count = result["paused"]
                if count > 0:
                    messages.success(
                        request,
                        _("Paused %(count)d items across all tickets.")
                        % {"count": count},
                    )
                else:
                    messages.warning(
                        request,
                        _("No active sessions were found to pause."),
                    )

            elif action == ProductionActions.FINISH_LINE:
                result = AssemblyLineService.finish_line(assembly_line, employee)
                count = result["finished"]
                if count > 0:
                    messages.success(
                        request,
                        _("Finished %(count)d items across all tickets.")
                        % {"count": count},
                    )
                else:
                    messages.warning(
                        request,
                        _("No in-progress items were found to finish."),
                    )

            else:
                messages.error(request, _("Unknown action."))
                return redirect(redirect_url)

            for error_msg in result.get("errors", []):
                messages.error(request, error_msg)

        except ValidationError as exc:
            messages.error(request, str(exc.message))
        except Exception as exc:
            logger.exception("Assembly line action error")
            messages.error(request, str(exc))

        return redirect(redirect_url)
