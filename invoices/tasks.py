from celery import shared_task
from django.conf import settings
from django.urls import reverse
from .models import IncomingInvoice, Invoice
from invoices.services.invoice.invoice_service import InvoiceService
from invoices.services.ocr_services.gemini_ocr import GeminiInvoiceProcessor
from core.services.email_service import EmailService
from google.genai.errors import ClientError
import logging
import tempfile
import os
import shutil
from company.models import CompanyEmailConfig
from .services.email_processor import EmailInvoiceProcessor
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

@shared_task(name="invoices.tasks.fetch_email_invoices")
def fetch_email_invoices_task(config_id=None):
    """
    Background task to fetch invoices from configured email accounts.
    If config_id is provided, only that configuration is processed.
    """
    if config_id:
        configs = CompanyEmailConfig.objects.filter(id=config_id)
    else:
        configs = CompanyEmailConfig.objects.all()

    if not configs.exists():
        logger.info("No email configurations to process.")
        return {"task": "fetch_email_invoices", "status": "skipped", "detail": "no configurations"}

    total_count = 0
    for config in configs:
        logger.info(f"Fetching emails for {config.email_address} ({config.company})")
        processor = EmailInvoiceProcessor(config)
        try:
            processor.connect()
            count = processor.fetch_new_invoices()
            processor.disconnect()
            total_count += count
        except Exception as e:
            logger.error(f"Error fetching emails for {config.email_address}: {str(e)}")
            continue

    return {"task": "fetch_email_invoices", "status": "ok", "fetched": total_count}

#Email Only Pipeline
@shared_task(name="invoices.tasks.process_incoming_invoices")
def process_incoming_invoices(invoice_ids=None):
    """
    Background task to process IncomingInvoice records.
    If invoice_ids is provided, processes those specific records.
    Otherwise, processes all unprocessed records.
    """
    if invoice_ids:
        invoices = IncomingInvoice.objects.filter(id__in=invoice_ids, processed=False, error="")
    else:
        invoices = IncomingInvoice.objects.filter(processed=False, error="")
    
    if not invoices.exists():
        logger.info("No new invoices to process.")
        return {"task": "process_incoming_invoices", "status": "skipped", "detail": "no new invoices"}

    logger.info(f"Found {invoices.count()} invoices to process.")
    
    # Initialize services
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        logger.error("GEMINI_API_KEY not configured.")
        return {"task": "process_incoming_invoices", "status": "error", "detail": "missing API key"}
        
    ocr_processor = GeminiInvoiceProcessor(api_key=api_key)
    service = InvoiceService(ocr_processor=ocr_processor)
    
    processed_count = 0
    errors_count = 0
    skipped_count = 0
    
    
    
    for incoming in invoices:
        temp_file_path = None
        try:
            logger.info(f"Processing invoice {incoming.id} (File: {incoming.file.name})")
            
            # Try to get local path, or download to temp
            try:
                file_path = incoming.file.path
            except NotImplementedError:
                suffix = os.path.splitext(incoming.file.name)[1]
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    with incoming.file.open('rb') as f:
                        shutil.copyfileobj(f, tmp)
                    temp_file_path = tmp.name
                    file_path = temp_file_path
            result = service.process_invoice(file_path, sender_email=incoming.sender_email)
            
            if result.success:
                incoming.processed = True
                incoming.is_duplicate = getattr(result, 'is_duplicate', False)
                incoming.save()
                processed_count += 1
                logger.info(f"Successfully processed invoice {incoming.id} (Duplicate: {incoming.is_duplicate})")
            elif result.invoice is None:
                # Document is not an invoice (quote, delivery note, etc.) — skip it
                incoming.processed = True
                incoming.save()
                skipped_count += 1
                logger.info(f"Skipped incoming {incoming.id}: {result.messages[-1] if result.messages else 'not an invoice'}")
            else:
                errors_count += 1
                error_msg = str(result.messages) if result.messages else "Unknown processing error"
                logger.error(f"Failed to process invoice {incoming.id}: {result.messages}")
                incoming.processed = True
                incoming.error = error_msg
                incoming.save(update_fields=["processed", "error"])
                
        except ClientError as e:
            error_msg = str(e)
            logger.error(f"ClientError processing invoice {incoming.id}: {error_msg}")
            incoming.error = error_msg
            incoming.processed = True
            incoming.save(update_fields=["error", "processed"])
            errors_count += 1
        except Exception as e:
            errors_count += 1
            error_msg = str(e)
            logger.error(f"Exception processing invoice {incoming.id}: {error_msg}")
            incoming.error = error_msg
            incoming.processed = True
            incoming.save(update_fields=["error", "processed"])
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                os.remove(temp_file_path)
            
    return {"task": "process_incoming_invoices", "status": "ok" if not errors_count else "partial", "processed": processed_count, "skipped": skipped_count, "errors": errors_count}

# Manual Upload Pipeline
@shared_task(name="invoices.tasks.process_manual_invoice")
def process_manual_invoice(invoice_id: int):
    """
    Background task to process a single manually uploaded Invoice.
    It uses the same OCR + service as email-based IncomingInvoice processing,
    but starts from the Invoice model instead.
    """
    print(f"Processing manual invoice {invoice_id}")
    invoice = Invoice.objects.get(pk=invoice_id)
    invoice.ocr_processing = True
    invoice.save(update_fields=["ocr_processing"])

    try:
        invoice = Invoice.objects.get(pk=invoice_id)
    except Invoice.DoesNotExist:
        logger.error(f"Invoice with id={invoice_id} does not exist.")
        return {"task": "process_manual_invoice", "invoice_id": invoice_id, "status": "error", "detail": "not found"}

    if not invoice.file:
        logger.error(f"Invoice {invoice_id} has no file attached.")
        # Update status flags
        invoice.ocr_processed = False
        invoice.ocr_processing = False
        invoice.ocr_error = "No file attached to invoice."
        invoice.save(update_fields=["ocr_processed", "ocr_processing", "ocr_error"])
        return {"task": "process_manual_invoice", "invoice_id": invoice_id, "status": "error", "detail": "no file attached"}


    # Initialize services (same as email task)
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        logger.error("GEMINI_API_KEY not configured.")
        invoice.ocr_processed = False
        invoice.ocr_processing = False
        invoice.ocr_error = "GEMINI_API_KEY not configured."
        invoice.save(update_fields=["ocr_processed", "ocr_processing", "ocr_error"])
        return {"task": "process_manual_invoice", "invoice_id": invoice_id, "status": "error", "detail": "missing API key"}
    
    ocr_processor = GeminiInvoiceProcessor(api_key=api_key)
    service = InvoiceService(ocr_processor=ocr_processor)

    temp_file_path = None

    invoice.ocr_processed = False
    invoice.ocr_error = ""
    invoice.save(update_fields=["ocr_processed", "ocr_error"])

    try:
        logger.info(f"Processing MANUAL invoice {invoice.id} (File: {invoice.file.name})")

        # Try to get local path, or download to temp (same pattern as IncomingInvoice)
        try:
            file_path = invoice.file.path
        except NotImplementedError:
            # S3 or other storage without local path access
            suffix = os.path.splitext(invoice.file.name)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                with invoice.file.open("rb") as f:
                    shutil.copyfileobj(f, tmp)
                temp_file_path = tmp.name
                file_path = temp_file_path

        result = service.process_existing_invoice(invoice, file_path=file_path)

        if result.success:
            logger.info(f"Successfully processed MANUAL invoice {invoice.id}")

            #update status
            invoice.ocr_processing = False
            invoice.ocr_processed = True
            invoice.ocr_error = ""
            invoice.save(update_fields=["ocr_processing", "ocr_processed", "ocr_error"])
            return {"task": "process_manual_invoice", "invoice_id": invoice_id, "status": "ok"}
        else:
            if getattr(result, 'is_duplicate', False):
                duplicate_of = getattr(result, 'duplicate_of', None)
                logger.warning(f"Duplicate MANUAL invoice {invoice.id}: {result.messages}")

                if invoice.uploaded_by and invoice.uploaded_by.email and duplicate_of:
                    original_link = f"{settings.BACKEND_URL.rstrip('/')}{reverse('admin:invoices_invoice_change', args=[duplicate_of.pk])}"
                    EmailService.send_email(
                        to=[invoice.uploaded_by.email],
                        subject=f"Duplicate invoice detected: {invoice.file.name}",
                        body_html=(
                            f"<p>The invoice you uploaded ({invoice.file.name}) is a duplicate of an "
                            f"invoice that already exists in the system and has been deleted.</p>"
                            f"<p><a href=\"{original_link}\">View the original invoice</a></p>"
                        ),
                        related_object=duplicate_of,
                        company=duplicate_of.company,
                    )

                invoice_id_for_log = invoice.id
                invoice.delete()
                return {"task": "process_manual_invoice", "invoice_id": invoice_id_for_log, "status": "duplicate_deleted"}

            logger.error(
                f"Failed to process MANUAL invoice {invoice.id}: {result.messages}"
            )
            #processing finished but with error
            invoice.ocr_processing = False
            invoice.ocr_processed = False
            invoice.ocr_error = str(result.messages)
            invoice.save(update_fields=["ocr_processing", "ocr_processed", "ocr_error"])
            return {"task": "process_manual_invoice", "invoice_id": invoice_id, "status": "error", "detail": str(result.messages)}

    except Exception as e:
        logger.error(f"Exception processing MANUAL invoice {invoice.id}: {str(e)}")

        # Unexpected exception: also mark as error
        invoice.ocr_processing = False
        invoice.ocr_processed = False
        invoice.ocr_error = str(e)
        invoice.save(update_fields=["ocr_processing", "ocr_processed", "ocr_error"])
        raise  # Re-raise to let Celery/Sentry capture the error

    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)


@shared_task(name="invoices.tasks.build_invoice_pdf_zip")
def build_invoice_pdf_zip_task(kind: str, invoice_ids: list[int], user_id: int | None = None):
    """
    Build a ZIP of sales or purchase invoice PDFs in the background and store
    it for later download from the admin status page.
    """
    from invoices.services.invoice_pdf_download import InvoicePdfDownloadService

    logger.info(
        "Building invoice PDF zip kind=%s count=%s user_id=%s",
        kind, len(invoice_ids or []), user_id,
    )
    return InvoicePdfDownloadService.build_and_store_zip(
        kind=kind,
        invoice_ids=list(invoice_ids or []),
        user_id=user_id,
    )

@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def suggest_ai_po_matches_task(self, invoice_id: int):
    """
    Run GeminiPOItemMatcher for a single Invoice. Deletes any PENDING
    AIMatchSuggestion rows for this invoice first, then regenerates from
    scratch — so a re-run never leaves stale suggestions mixed with new ones.

    Only "high" confidence matches from Gemini are persisted; "medium"/"low"
    are discarded (Gemini uses them internally for calibration only).

    Returns a result dict consumed by the matcher status page:
        {"status": "done", "invoice_id": X, "has_match": bool, "suggestions_count": N}
        {"status": "no_match", "invoice_id": X, "has_match": False, "suggestions_count": 0}
    """
    from django.conf import settings

    from invoices.models import (
        AIMatchSuggestion,
        AIMatchSuggestionStatus,
        Invoice,
        InvoiceItem,
    )
    from invoices.services.gemini_po_matcher import GeminiPOItemMatcher
    from invoices.services.po_matching import POInvoiceMatchService
    from purchasing.models import PurchaseOrderItem

    try:
        invoice = Invoice.objects.get(pk=invoice_id)

        AIMatchSuggestion.objects.filter(
            invoice=invoice, status=AIMatchSuggestionStatus.PENDING
        ).delete()

        invoice_items_data, po_candidates_data = POInvoiceMatchService.ai_match_pool(invoice)

        if not invoice_items_data or not po_candidates_data:
            logger.info(
                "AI PO matcher: nothing to match for invoice %s (no unmatched lines "
                "or no candidate POs with pending quantity).",
                invoice_id,
            )
            return {
                "status": "no_match",
                "invoice_id": invoice_id,
                "has_match": False,
                "suggestions_count": 0,
            }

        matcher = GeminiPOItemMatcher(api_key=settings.GEMINI_API_KEY)
        raw_matches = matcher.find_matches(invoice_items_data, po_candidates_data)

        invoice_item_ids = {item["id"] for item in invoice_items_data}
        po_item_ids = {
            pi["id"] for po in po_candidates_data for pi in po["items"]
        }

        created = []
        for match in raw_matches:
            if match.get("confidence") != "high":
                continue
            if match.get("invoice_item_id") not in invoice_item_ids:
                continue
            if match.get("po_item_id") not in po_item_ids:
                continue
            try:
                quantity = Decimal(str(match["quantity"]))
            except (InvalidOperation, KeyError, TypeError):
                continue
            if quantity <= 0:
                continue

            created.append(AIMatchSuggestion(
                invoice=invoice,
                invoice_item_id=match["invoice_item_id"],
                po_item_id=match["po_item_id"],
                quantity=quantity,
                confidence=match["confidence"],
                reasoning=match.get("reasoning", ""),
                raw_response=match,
            ))

        AIMatchSuggestion.objects.bulk_create(created)

        logger.info(
            "AI PO matcher ran for invoice %s: %d high-confidence suggestion(s) "
            "out of %d raw match(es).",
            invoice_id, len(created), len(raw_matches),
        )
        return {
            "status": "done" if created else "no_match",
            "invoice_id": invoice_id,
            "has_match": bool(created),
            "suggestions_count": len(created),
        }
    except Invoice.DoesNotExist:
        logger.error("Invoice %s does not exist, skipping AI PO matcher.", invoice_id)
        return {"status": "error", "message": f"Invoice {invoice_id} not found."}
    except Exception as exc:
        logger.exception("AI PO matcher failed for invoice %s", invoice_id)
        raise self.retry(exc=exc)



# ── Pending invoices daily digest ─────────────────────────────────────
# Data: invoices.services.pending_invoices_digest_service
# Email construction: invoices.services.pending_invoices_digest_email_service
# This task only orchestrates: fetch -> build -> send -> report.


@shared_task(name="invoices.tasks.send_pending_invoices_digest")
def send_pending_invoices_digest():
    """Daily task: emails a bank-report-style digest of pending invoices —
    what each company owes its providers (unpaid/partially-paid purchase
    invoices, credit notes netted) and what its clients owe it (final,
    unpaid sales invoices) — to Company.invoice_digest_emails recipients.

    Recipients configured on several companies receive ONE consolidated
    email covering all of their companies, with a global totals block
    (to pay / to receive / net position) and links to the corresponding
    admin changelists pre-filtered to each company's pending documents.

    Companies with no recipients configured are skipped — opt-in per
    company, like banking.tasks.send_bank_accounts_summary.

    Not wired into settings.CELERY_BEAT_SCHEDULE: schedule it via a
    django-celery-beat PeriodicTask (admin > Periodic Tasks, e.g. daily
    at 07:00) pointing at "invoices.tasks.send_pending_invoices_digest".
    """
    from django.utils import timezone

    from company.models import Company
    from invoices.services.pending_invoices_digest_email_service import (
        PendingInvoicesDigestEmailService,
    )
    from invoices.services.pending_invoices_digest_service import (
        PendingInvoicesDigestService,
    )

    today = timezone.localdate()
    companies = list(Company.objects.exclude(invoice_digest_emails=[]))
    digests = PendingInvoicesDigestService.get_company_digests(companies, today=today)
    emails = PendingInvoicesDigestEmailService.build_emails(
        companies, digests, today=today
    )

    emails_sent = 0
    errors = []

    for email in emails:
        recipients_label = ", ".join(email.recipients)
        try:
            success = EmailService.send_email(
                to=email.recipients,
                subject=email.subject,
                body_html=email.body_html,
                related_object=email.company,
                company=email.company,
            )
            if success:
                emails_sent += 1
            else:
                errors.append(f"Failed to send invoices digest to [{recipients_label}]")
        except Exception as e:
            errors.append(
                f"Error sending invoices digest to [{recipients_label}]: {str(e)}"
            )

    if errors:
        raise Exception(
            f"Pending invoices digest task completed with errors: {'; '.join(errors)}"
        )

    return {
        "task": "send_pending_invoices_digest",
        "status": "ok",
        "emails_sent": emails_sent,
    }
