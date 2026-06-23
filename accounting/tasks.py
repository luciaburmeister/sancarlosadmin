from celery import shared_task
from django.conf import settings
import logging

from .services.accounting.account_classifier_service import AccountClassifierService
from .services.accounting.gemini_account_classifier import GeminiAccountClassifier
from .services.journal_generator import JournalGeneratorService
from .services.a3_exporter import A3PurchaseExportService
from company.models import Company
from datetime import date
from django.core.mail import EmailMessage


logger = logging.getLogger(__name__)


@shared_task(name="accounting.tasks.classify_invoice_accounts")
def classify_invoice_accounts(invoice_ids: list = None):
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
        return {"task": "classify_invoice_accounts", "status": "error", "detail": "missing API key"}

    classifier = GeminiAccountClassifier(api_key=api_key)
    service = AccountClassifierService(classifier=classifier)

    try:
        summary = service.classify_unclassified_invoices(invoice_ids=invoice_ids)
        logger.info(f"Classification complete: {summary}")
        return {"task": "classify_invoice_accounts", "status": "ok", "summary": str(summary)}

    except Exception as e:
        logger.error(f"Account classification task failed: {str(e)}")
        raise

@shared_task(name="accounting.tasks.generate_journal_entries_task")
def generate_journal_entries_task(company_id=None):
    """
    Daily task to generate journal entries for fully classified invoices.
    """
    logger.info("Starting daily journal entry generation...")
    
    if company_id:
        companies = Company.objects.filter(id=company_id)
    else:
        companies = Company.objects.filter(is_active=True)

    total_processed = 0
    total_unbalanced = 0
    all_errors = []
    
    for company in companies:
        generator = JournalGeneratorService(company)
        result = generator.journalize_all_pending()
        
        count = result.get("processed", 0)
        unbalanced = result.get("unbalanced", 0)
        errors = result.get("errors", [])
        
        total_processed += count
        total_unbalanced += unbalanced
        all_errors.extend(errors)
        
        if count > 0:
            logger.info(f"Journalized {count} invoices for {company.name}")
        if unbalanced > 0:
            logger.info(f"Created {unbalanced} DRAFT unbalanced entries for {company.name}")
        if errors:
            logger.error(f"Encountered {len(errors)} errors for {company.name}: {errors}")

    return {
        "task": "generate_journal_entries",
        "status": "ok" if not all_errors else "partial",
        "journalized": total_processed,
        "draft_unbalanced": total_unbalanced,
        "errors": all_errors,
    }

@shared_task(name="accounting.tasks.export_a3_purchase_invoicing_task")
def export_a3_purchase_invoicing_task(company_id, user_email, start_date_str, end_date_str):
    """
    Export purchase invoicing to A3 format and email it.
    Dates should be in 'YYYY-MM-DD' format.
    """
    logger.info(f"Starting A3 export for company {company_id} to {user_email}")
    
    try:
        company = Company.objects.get(id=company_id)
        start_date = date.fromisoformat(start_date_str)
        end_date = date.fromisoformat(end_date_str)
        
        # 1. Generate the Excel
        exporter = A3PurchaseExportService(company, start_date, end_date)
        excel_content = exporter.generate_excel_report()
        
        # 2. Prepare Email
        subject = f"Export A3 - {company.name} ({start_date_str} to {end_date_str})"
        body = f"Adjunto enviamos el archivo de exportación para A3 correspondiente al periodo solicitado.\n\nEmpresa: {company.name}\nPeriodo: {start_date_str} - {end_date_str}"
        
        email = EmailMessage(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user_email]
        )
        
        filename = f"export_a3_{company.name.lower().replace(' ', '_')}_{start_date_str}_{end_date_str}.xlsx"
        email.attach(filename, excel_content, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        
        email.send()
        
        return {
            "task": "export_a3_purchase_invoicing",
            "status": "ok",
            "company": company.name,
            "to": user_email,
            "period": f"{start_date_str} to {end_date_str}",
        }
        
    except Company.DoesNotExist:
        return {"task": "export_a3_purchase_invoicing", "status": "error", "detail": f"company {company_id} not found"}
    except Exception as e:
        logger.error(f"A3 export task failed: {str(e)}")
        return {"task": "export_a3_purchase_invoicing", "status": "error", "detail": str(e)}