from django.shortcuts import get_object_or_404, reverse
from django.views import View
from django.utils.translation import gettext as _
from unfold.views import UnfoldModelAdminViewMixin
from django.http import HttpResponse
from django.http import HttpRequest
from production.models import ProductionTicket
class ProductionTicketHtmlRender(UnfoldModelAdminViewMixin, View):
    permission_required = ("production.view_productionticket",)
    def get(self, request: HttpRequest, ticket_id: int):
        ticket = get_object_or_404(ProductionTicket, pk=ticket_id)
        
        stages_url = reverse("admin:production_productionticket_stages", args=[ticket_id])
        button_label = _("Return to Stages")
        
        # Inject floating button and no-print styles
        injected_html = f"""
        <style>
            @media print {{
                .no-print {{
                    display: none !important;
                }}
            }}
            .floating-nav-btn {{
                position: fixed;
                bottom: 30px;
                right: 30px;
                background-color: #2563eb;
                color: white;
                padding: 12px 24px;
                border-radius: 9999px;
                text-decoration: none;
                font-weight: 600;
                box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1), 0 24px 10px -5px rgb(0 0 0 / 0.06);
                z-index: 9999;
                transition: transform 0.2s;
            }}
            .floating-nav-btn:hover {{
                transform: scale(1.05);
                background-color: #1d4ed8;
            }}
        </style>
        <a href="{stages_url}" class="floating-nav-btn no-print">
            {button_label}
        </a>
        """
        
        full_html = ticket.order_html + injected_html
        
        return HttpResponse(
            full_html,
            content_type="text/html; charset=utf-8"
        )
