from django.shortcuts import get_object_or_404
from django.views import View
from django.utils.translation import gettext as _
from unfold.views import UnfoldModelAdminViewMixin
from django.http import HttpResponse
from django.http import HttpRequest
from production.models import ProductionTicket
class ProductionTicketHtmlRender(UnfoldModelAdminViewMixin, View):
    permission_required = ("production.view_productionticket",)
    def get(self, get: HttpRequest, ticket_id: int):
        ticket = get_object_or_404(ProductionTicket, pk=ticket_id)
        return HttpResponse(
            ticket.order_html,
            content_type="text/html; charset=utf-8"
        )
