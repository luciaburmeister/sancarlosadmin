import logging
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework import status
from rest_framework_api_key.permissions import HasAPIKey

from sales.services.cortizo_import import CortizoImportService

logger = logging.getLogger(__name__)


@api_view(["POST"])
@permission_classes([HasAPIKey])
def cortizo_import_view(request):
    """
    Import Cortizo sales documents from JSON payload.

    Expects a JSON array of sales documents (same format as extracted_data.json).
    Protected by API key authentication (Authorization: Api-Key <key>).

    Query params:
        company (str): Company name to import for. Defaults to "Aluares".
    """
    company_name = request.query_params.get("company", "Aluares")
    data = request.data

    if not isinstance(data, list):
        return Response(
            {"error": "Expected a JSON array of sales documents."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not data:
        return Response(
            {"error": "Empty document list."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        service = CortizoImportService(company_name=company_name)
        quotes = service.import_documents(data)

        results = []
        for quote in quotes:
            results.append({
                "quote_id": quote.id,
                "client": quote.client.name,
                "project_name": quote.project_name,
                "status": quote.status,
            })

        logger.info(
            "Cortizo import completed: %d documents → %d quotes for company '%s'",
            len(data), len(quotes), company_name,
        )

        return Response({
            "imported": len(quotes),
            "documents_received": len(data),
            "company": company_name,
            "quotes": results,
        }, status=status.HTTP_200_OK)

    except Exception as e:
        logger.exception("Cortizo import failed: %s", str(e))
        return Response(
            {"error": f"Import failed: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
