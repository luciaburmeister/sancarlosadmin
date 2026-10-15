from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View

from core.documents import enqueue_document
from sales.documents import render_quote_pdf
from sales.models import Quote


class QuotePdfDownloadView(View):
    """Download a WeasyPrint PDF of a quote using its default quotation template.

    The render runs on the pdf Celery queue; the user waits on the document
    status page and the download starts from there.
    """

    model_admin = None

    def get(self, request, quote_id):
        quote = get_object_or_404(Quote, pk=quote_id)
        return enqueue_document(
            request,
            render_quote_pdf,
            quote_id=quote.pk,
            filename=f"Presupuesto_{quote.pk}.pdf",
            back_url=reverse("admin:sales_quote_change", args=[quote.pk]),
        )
