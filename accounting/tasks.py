from celery import shared_task
from django.conf import settings
import logging

from .services.accounting.account_classifier_service import AccountClassifierService
from .services.accounting.gemini_account_classifier import GeminiAccountClassifier


logger = logging.getLogger(__name__)


@shared_task
def classify_invoice_accounts():
    """
    Background task to classify all unclassified invoices into
    accounting accounts using Gemini.

    Processes all invoices where:
    - account is NULL (not yet classified)
    - ocr_processed is True (OCR has already run)
    - line_items is not empty (something to classify)

    This task is batch-based: one single Gemini call for all
    unclassified invoices at once.

    Can be triggered:
    - Manually:   classify_invoice_accounts.delay()
    - On a schedule via Celery Beat (add to CELERY_BEAT_SCHEDULE in settings)
    """
    logger.info("Starting account classification task...")

    api_key = settings.GEMINI_API_KEY
    if not api_key:
        logger.error("GEMINI_API_KEY not configured.")
        return "Missing API Key"

    classifier = GeminiAccountClassifier(api_key=api_key)
    service = AccountClassifierService(classifier=classifier)

    try:
        summary = service.classify_unclassified_invoices()
        logger.info(f"Classification complete: {summary}")
        return str(summary)

    except Exception as e:
        logger.error(f"Account classification task failed: {str(e)}")
        raise