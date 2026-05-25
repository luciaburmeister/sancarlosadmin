from django.db import transaction
from django.http import JsonResponse

from api._utils import CompanyAPIView
from invoices.models import SalesInvoice
from invoices.services.sales_invoice_service import SalesInvoiceService
from sales.models import Quote


class QuoteInvoiceView(CompanyAPIView):
    def post(self, request, quote_id):
        try:
            quote = Quote.objects.prefetch_related("configurations").get(
                pk=quote_id, company=self.company
            )
        except Quote.DoesNotExist:
            return JsonResponse({"error": "Presupuesto no encontrado."}, status=404)

        configs = list(quote.configurations.all())
        if not configs:
            return JsonResponse(
                {"error": "El presupuesto no tiene servicios configurados."}, status=400
            )

        invoice_ids = []
        already_existed = True

        try:
            with transaction.atomic():
                for config in configs:
                    existing = (
                        SalesInvoice.objects.filter(configuration=config)
                        .order_by("-id")
                        .first()
                    )
                    if existing:
                        invoice_ids.append(existing.id)
                    else:
                        already_existed = False
                        invoice = SalesInvoiceService.create_from_configuration(config)
                        invoice_ids.append(invoice.id)

                if quote.status != "accepted":
                    quote.status = "accepted"
                    quote.save(update_fields=["status", "updated_at"])
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=400)

        return JsonResponse(
            {
                "invoice_ids": invoice_ids,
                "already_existed": already_existed,
                "quote_status": quote.status,
            },
            status=200 if already_existed else 201,
        )
