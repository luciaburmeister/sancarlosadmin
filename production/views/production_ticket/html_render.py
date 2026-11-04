from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views import View
from django.views.decorators.clickjacking import xframe_options_exempt
from unfold.views import UnfoldModelAdminViewMixin


@method_decorator(xframe_options_exempt, name='dispatch')
class ProductionTicketDocumentView(UnfoldModelAdminViewMixin, View):
    """A standalone page showing one of a ticket's documents with shop-floor navigation."""

    permission_required = ("production.view_productionticket",)
    document_template_name = "admin/production/productionticket/ticket_document.html"
    nav_template_name = "admin/production/productionticket/ticket_document_nav.html"

    def get_ticket(self):
        return get_object_or_404(self.model_admin.get_queryset(self.request), pk=self.kwargs["ticket_id"])

    def get_nav_context(self, ticket):
        return {"ticket": ticket}

    def render_document(self, ticket, file, *, title, is_image=False):
        context = {
            **self.get_nav_context(ticket),
            "title": title,
            "file_url": file.url,
            "is_image": is_image,
        }
        return HttpResponse(render_to_string(self.document_template_name, context, request=self.request))


class ProductionTicketHtmlRender(ProductionTicketDocumentView):
    """The work order: the supplier PDF when present, otherwise the rendered production order."""

    def get_nav_context(self, ticket):
        return {**super().get_nav_context(ticket), "show_drawings_link": True}

    def get(self, request: HttpRequest, *args, **kwargs):
        ticket = self.get_ticket()
        if ticket.work_order_pdf:
            return self.render_document(ticket, ticket.work_order_pdf, title=_("Work Order"))

        # order_html is a complete rendered document; the navigation is appended after it.
        nav_html = render_to_string(self.nav_template_name, self.get_nav_context(ticket), request=request)
        return HttpResponse((ticket.order_html or "") + nav_html)


class ProductionTicketDrawingsView(ProductionTicketDocumentView):
    def get_nav_context(self, ticket):
        return {**super().get_nav_context(ticket), "show_work_order_link": True}

    def get(self, request: HttpRequest, *args, **kwargs):
        ticket = self.get_ticket()
        if not ticket.drawings_file:
            raise Http404
        return self.render_document(
            ticket, ticket.drawings_file, title=_("Drawings"), is_image=ticket.drawings_is_image,
        )
