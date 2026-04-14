from django.shortcuts import get_object_or_404, reverse
from django.views import View
from django.utils.translation import gettext as _
from unfold.views import UnfoldModelAdminViewMixin
from django.http import HttpResponse
from django.http import HttpRequest
from django.utils.decorators import method_decorator
from django.views.decorators.clickjacking import xframe_options_exempt
from production.models import ProductionTicket
@method_decorator(xframe_options_exempt, name='dispatch')
class ProductionTicketHtmlRender(UnfoldModelAdminViewMixin, View):
    permission_required = ("production.view_productionticket",)
    def get(self, request: HttpRequest, ticket_id: int):
        ticket = get_object_or_404(ProductionTicket, pk=ticket_id)
        
        stages_url = reverse("admin:production_productionticket_stages", args=[ticket_id])
        button_label = _("Return to Stages")
        
        # Style for the floating button
        style_html = f"""
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

        if ticket.work_order_pdf:
            pdf_url = ticket.work_order_pdf.url
            full_html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>{_("Work Order")} #{ticket.pk}</title>
                {style_html}
                <style>
                    body, html {{
                        margin: 0;
                        padding: 0;
                        height: 100%;
                        overflow: hidden;
                    }}
                    iframe {{
                        width: 100%;
                        height: 100%;
                        border: none;
                    }}
                </style>
            </head>
            <body>
                <iframe src="{pdf_url}"></iframe>
            </body>
            </html>
            """
            return HttpResponse(full_html, content_type="text/html; charset=utf-8")

        # Inject floating button and no-print styles
        full_html = (ticket.order_html or "") + style_html
        
        return HttpResponse(
            full_html,
            content_type="text/html; charset=utf-8"
        )
