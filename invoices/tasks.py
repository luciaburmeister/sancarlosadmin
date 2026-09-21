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
                        email_document="duplicate_invoice_alert",
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

@shared_task(bind=True, max_retries=2, default_retry_delay=60)
def suggest_ai_invoice_matches_task(self, po_id: int):
    """
    Mirror of suggest_ai_po_matches_task, run from the PO side: finds
    candidate invoices for a single PurchaseOrder and asks Gemini which of
    their lines match this PO's pending lines. Deletes any PENDING
    AIMatchSuggestion rows tied to this PO first, then regenerates from
    scratch — same idempotency guarantee as the invoice-side task.

    Only "high" confidence matches from Gemini are persisted; "medium"/"low"
    are discarded (Gemini uses them internally for calibration only).

    A single run can produce suggestions spanning several different
    invoices (this PO's lines may each be billed by a different invoice),
    so AIMatchSuggestion.invoice is set per-suggestion from the matched
    invoice_item, not from a single invoice_id passed to the task.

    Returns a result dict consumed by the matcher status page:
        {"status": "done", "po_id": X, "has_match": bool, "suggestions_count": N}
        {"status": "no_match", "po_id": X, "has_match": False, "suggestions_count": 0}
    """
    from django.conf import settings

    from invoices.models import AIMatchSuggestion, AIMatchSuggestionStatus, InvoiceItem
    from invoices.services.gemini_po_matcher import GeminiPOItemMatcher
    from invoices.services.po_matching import POInvoiceMatchService
    from purchasing.models import PurchaseOrder, PurchaseOrderItem

    try:
        po = PurchaseOrder.objects.get(pk=po_id)

        AIMatchSuggestion.objects.filter(
            po_item__purchase_order=po, status=AIMatchSuggestionStatus.PENDING
        ).delete()

        po_items_data, invoice_candidates_data = POInvoiceMatchService.ai_match_pool_for_po(po)

        if not po_items_data or not invoice_candidates_data:
            logger.info(
                "AI invoice matcher: nothing to match for PO %s (no unmatched lines "
                "or no candidate invoices with pending quantity).",
                po_id,
            )
            return {
                "status": "no_match",
                "po_id": po_id,
                "has_match": False,
                "suggestions_count": 0,
            }

        matcher = GeminiPOItemMatcher(api_key=settings.GEMINI_API_KEY)
        raw_matches = matcher.find_matches_for_po(po_items_data, invoice_candidates_data)

        po_item_ids = {item["id"] for item in po_items_data}
        invoice_item_ids = {
            item["id"] for invoice in invoice_candidates_data for item in invoice["items"]
        }
        invoice_id_by_item_id = {
            item["id"]: invoice["invoice_id"]
            for invoice in invoice_candidates_data
            for item in invoice["items"]
        }

        created = []
        for match in raw_matches:
            if match.get("confidence") != "high":
                continue
            if match.get("po_item_id") not in po_item_ids:
                continue
            if match.get("invoice_item_id") not in invoice_item_ids:
                continue
            try:
                quantity = Decimal(str(match["quantity"]))
            except (InvalidOperation, KeyError, TypeError):
                continue
            if quantity <= 0:
                continue

            created.append(AIMatchSuggestion(
                invoice_id=invoice_id_by_item_id[match["invoice_item_id"]],
                invoice_item_id=match["invoice_item_id"],
                po_item_id=match["po_item_id"],
                quantity=quantity,
                confidence=match["confidence"],
                reasoning=match.get("reasoning", ""),
                raw_response=match,
            ))

        AIMatchSuggestion.objects.bulk_create(created)

        logger.info(
            "AI invoice matcher ran for PO %s: %d high-confidence suggestion(s) "
            "out of %d raw match(es).",
            po_id, len(created), len(raw_matches),
        )
        return {
            "status": "done" if created else "no_match",
            "po_id": po_id,
            "has_match": bool(created),
            "suggestions_count": len(created),
        }
    except PurchaseOrder.DoesNotExist:
        logger.error("PurchaseOrder %s does not exist, skipping AI invoice matcher.", po_id)
        return {"status": "error", "message": f"PurchaseOrder {po_id} not found."}
    except Exception as exc:
        logger.exception("AI invoice matcher failed for PO %s", po_id)
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
                email_document="pending_invoices_digest",
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


# ── Unmatched PO daily digest ─────────────────────────────────────────
# Data: invoices.services.unmatched_po_digest_service
# Email construction: invoices.services.unmatched_po_digest_email_service
# This task only orchestrates: fetch -> build -> send -> report.


@shared_task(name="invoices.tasks.send_unmatched_po_digest")
def send_unmatched_po_digest(grace_hours: int | None = None, start_date=None):
    """Daily task (POL-249): emails a report of purchase invoices that are
    still not linked to a purchase order — fully unmatched or only
    partially allocated — more than 48 hours after entering the ERP, to
    Company.unmatched_po_digest_emails recipients.

    Invoices flagged "No relevant PO" (Invoice.skip_po_matching) are
    excluded: that flag is how the ERP marks a purchase as exempt from
    needing a PO (fuel, landfill fees, bank charges). Credit notes are
    excluded too — they reverse spend rather than commit it.

    Recipients configured on several companies receive ONE consolidated
    email covering all of them. Recipient groups with nothing to report
    get no email — a daily "all clear" would train people to ignore it.

    Each company sets its own grace period and start date on its Company
    record (unmatched_po_digest_grace_hours, defaulting to 48, and
    unmatched_po_digest_start_date) — so turning the digest on does not
    surface years of historical invoices, and the threshold is editable
    from the admin without a deploy. The task arguments override both for
    every company, and exist for one-off previews rather than routine use.

    Not wired into settings.CELERY_BEAT_SCHEDULE: schedule it via a
    django-celery-beat PeriodicTask (admin > Periodic Tasks, e.g. daily
    at 08:00) pointing at "invoices.tasks.send_unmatched_po_digest".
    """
    from django.utils import timezone

    from company.models import Company
    from invoices.services.unmatched_po_digest_email_service import (
        UnmatchedPODigestEmailService,
    )
    from invoices.services.unmatched_po_digest_service import (
        UnmatchedPODigestService,
    )

    today = timezone.localdate()
    companies = list(Company.objects.exclude(unmatched_po_digest_emails=[]))
    digests = UnmatchedPODigestService.get_company_digests(
        companies, grace_hours=grace_hours, start_date=start_date
    )
    emails = UnmatchedPODigestEmailService.build_emails(
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
                email_document="unmatched_po_digest",
            )
            if success:
                emails_sent += 1
            else:
                errors.append(
                    f"Failed to send unmatched PO digest to [{recipients_label}]"
                )
        except Exception as e:
            errors.append(
                f"Error sending unmatched PO digest to [{recipients_label}]: {str(e)}"
            )

    if errors:
        raise Exception(
            f"Unmatched PO digest task completed with errors: {'; '.join(errors)}"
        )

    return {
        "task": "send_unmatched_po_digest",
        "status": "ok",
        "emails_sent": emails_sent,
    }


@shared_task(name="invoices.tasks.process_payment_reminders")
def process_payment_reminders():
    """
    Celery-beat entry point (POL-xxx), scheduled every 30 minutes.

    For each company whose PaymentReminderSettings.send_time falls inside
    the current 30-minute window and hasn't run yet today, checks every
    eligible SalesInvoice against that company's PaymentReminderRule rows
    (pre-due) and overdue_repeat_days (post-due), creating a PENDING
    PaymentReminderLog and queuing send_payment_reminder_email for each
    reminder that's due. Idempotency is enforced by the unique constraint
    on (invoice, reminder_type, scheduled_for) — a duplicate get_or_create
    just returns the existing row without queuing another send.

    After processing a company it queues send_payment_reminder_report for
    that company and day, delayed so the individual sends have finished.
    """
    from datetime import datetime, timedelta

    from django.db.models import Exists, OuterRef
    from django.utils import timezone

    from invoices.models import (
        PaymentReminderSettings,
        PaymentReminderRule,
        PaymentReminderLog,
        PaymentReminderLogStatus,
        PaymentReminderType,
        SalesInvoice,
        SalesInvoiceStatus,
        InvoicePaymentStatus,
    )

    now = timezone.localtime()
    today = now.date()
    queued = 0
    processed_companies = 0

    settings_qs = PaymentReminderSettings.objects.filter(is_active=True).exclude(last_run_date=today)

    for reminder_settings in settings_qs.select_related("company"):
        scheduled_dt = timezone.make_aware(datetime.combine(today, reminder_settings.send_time))
        if not (scheduled_dt <= now < scheduled_dt + timedelta(minutes=30)):
            continue

        company = reminder_settings.company
        processed_companies += 1

        # Clients with an unsettled negative invoice (credit note) are skipped:
        # a negative invoice means we owe them, so no reminder is sent.
        client_has_open_negative_invoice = SalesInvoice.objects.filter(
            company=company,
            client=OuterRef("client"),
            status=SalesInvoiceStatus.FINAL,
            total_amount__lt=0,
        ).exclude(payment_status=InvoicePaymentStatus.PAID)

        invoices = (
            SalesInvoice.objects.filter(
                company=company,
                status=SalesInvoiceStatus.FINAL,
                due_date__isnull=False,
                exclude_from_payment_reminders=False,
                total_amount__gt=0,
            )
            .exclude(payment_status=InvoicePaymentStatus.PAID)
            .exclude(client__exclude_from_payment_reminders=True)
            .exclude(Exists(client_has_open_negative_invoice))
            .select_related("client")
        )

        rules = list(reminder_settings.rules.filter(is_active=True))

        for invoice in invoices:
            try:
                for rule in rules:
                    target_date = invoice.due_date - timedelta(days=rule.days_before_due)
                    if target_date != today:
                        continue
                    log, created = PaymentReminderLog.objects.get_or_create(
                        invoice=invoice,
                        reminder_type=PaymentReminderType.PRE_DUE,
                        scheduled_for=today,
                        defaults={"company": company, "rule": rule},
                    )
                    if created:
                        send_payment_reminder_email.delay(log.id)
                        queued += 1

                if invoice.due_date < today:
                    last_sent = (
                        PaymentReminderLog.objects.filter(
                            invoice=invoice,
                            reminder_type=PaymentReminderType.OVERDUE,
                            status=PaymentReminderLogStatus.SENT,
                        )
                        .order_by("-scheduled_for")
                        .first()
                    )
                    due_for_overdue_reminder = (
                        last_sent is None
                        or (today - last_sent.scheduled_for).days >= reminder_settings.overdue_repeat_days
                    )
                    if due_for_overdue_reminder:
                        log, created = PaymentReminderLog.objects.get_or_create(
                            invoice=invoice,
                            reminder_type=PaymentReminderType.OVERDUE,
                            scheduled_for=today,
                            defaults={"company": company, "rule": None},
                        )
                        if created:
                            send_payment_reminder_email.delay(log.id)
                            queued += 1
            except Exception:
                logger.exception("Failed to evaluate payment reminders for invoice %s", invoice.pk)

        reminder_settings.last_run_date = today
        reminder_settings.save(update_fields=["last_run_date"])

        # Report what happened once the queued sends have had time to run.
        send_payment_reminder_report.apply_async(
            args=[company.pk, today.isoformat()],
            countdown=PAYMENT_REMINDER_REPORT_DELAY_SECONDS,
        )

    logger.info(
        "Payment reminder check complete: %d companies processed, %d reminder(s) queued.",
        processed_companies, queued,
    )
    return {"companies_processed": processed_companies, "reminders_queued": queued}


# ── Payment reminder daily report ─────────────────────────────────────
# Email construction: invoices.services.payment_reminder_report_email_service
# This task only orchestrates: wait for sends -> fetch logs -> build -> send.

# Seconds between queuing a company's reminders and sending its report.
PAYMENT_REMINDER_REPORT_DELAY_SECONDS = 10 * 60
# If some reminders are still pending when the report runs, re-check this
# often, up to max_retries times, before reporting them as pending.
PAYMENT_REMINDER_REPORT_RETRY_SECONDS = 2 * 60


@shared_task(
    name="invoices.tasks.send_payment_reminder_report",
    bind=True,
    max_retries=5,
    default_retry_delay=PAYMENT_REMINDER_REPORT_RETRY_SECONDS,
)
def send_payment_reminder_report(self, company_id: int, report_date: str):
    """
    Emails one company's payment reminder report for a given day: every
    PaymentReminderLog scheduled for that date (automatic and manual),
    with the client, the address used, the invoice and the outcome —
    sent (accepted by the mail server), failed (with the error) or
    cancelled (invoice paid / excluded before the send).

    Queued by process_payment_reminders a few minutes after the reminders
    themselves. If any reminder is still pending, the task retries a few
    times so the report reflects final outcomes; after that it reports
    them as pending rather than never arriving.

    Recipients: PaymentReminderSettings.report_emails, falling back to
    Company.email. Companies with neither are skipped with a warning. No
    email is sent on days without any reminder activity.
    """
    from datetime import date as date_cls

    from invoices.models import (
        PaymentReminderLog,
        PaymentReminderLogStatus,
        PaymentReminderSettings,
    )
    from invoices.services.payment_reminder_report_email_service import (
        PaymentReminderReportEmailService,
    )

    day = date_cls.fromisoformat(report_date)

    try:
        reminder_settings = PaymentReminderSettings.objects.select_related("company").get(
            company_id=company_id
        )
    except PaymentReminderSettings.DoesNotExist:
        logger.error("No PaymentReminderSettings for company %s, skipping report.", company_id)
        return {"status": "error", "message": f"No reminder settings for company {company_id}."}

    company = reminder_settings.company
    logs = list(
        PaymentReminderLog.objects.filter(company=company, scheduled_for=day)
        .select_related("invoice", "invoice__client")
    )
    if not logs:
        return {"status": "skipped", "reason": "no reminders scheduled for this day"}

    pending = sum(1 for log in logs if log.status == PaymentReminderLogStatus.PENDING)
    if pending and self.request.retries < self.max_retries:
        logger.info(
            "Payment reminder report for company %s: %d reminder(s) still pending, retrying.",
            company.pk, pending,
        )
        raise self.retry(countdown=PAYMENT_REMINDER_REPORT_RETRY_SECONDS)

    recipients = reminder_settings.report_recipients()
    if not recipients:
        logger.warning(
            "Payment reminder report for company %s not sent: no report emails configured "
            "and the company has no email.",
            company.pk,
        )
        return {"status": "skipped", "reason": "no recipients"}

    email = PaymentReminderReportEmailService.build_email(
        company, logs, recipients=recipients, report_date=day
    )
    success = EmailService.send_email(
        to=email.recipients,
        subject=email.subject,
        body_html=email.body_html,
        related_object=company,
        company=company,
        email_document="payment_reminder_report",
    )
    if not success:
        raise Exception(
            f"Failed to send payment reminder report for company {company.pk} to [{', '.join(recipients)}]"
        )

    return {
        "task": "send_payment_reminder_report",
        "status": "ok",
        "company_id": company.pk,
        "reminders": len(logs),
        "pending": pending,
        "recipients": recipients,
    }

# process_payment_reminders queues every due reminder at once. Each send
# renders the invoice PDF, so the burst is spread out rather than hitting the
# PDF worker (and the SMTP server) all at the same minute. At this rate the
# report task's retry window (10 min delay + 5 retries of 2 min) still covers
# several hundred reminders per company.
PAYMENT_REMINDER_SEND_RATE_LIMIT = "20/m"


@shared_task(
    name="invoices.tasks.send_payment_reminder_email",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    rate_limit=PAYMENT_REMINDER_SEND_RATE_LIMIT,
)
def send_payment_reminder_email(self, log_id: int):
    """
    Sends the actual reminder email for a single PaymentReminderLog and
    updates its status. Re-checks the invoice right before sending so a
    payment or exclusion applied after the log was queued still stops it.

    Routed to the "pdf" queue (settings.CELERY_TASK_ROUTES) and rate limited:
    it renders a WeasyPrint PDF per invoice.
    """
    from django.template.loader import render_to_string
    from django.utils import timezone, translation

    from invoices.models import (
        PaymentReminderLog,
        PaymentReminderLogStatus,
        PaymentReminderType,
        InvoicePaymentStatus,
        resolve_reminder_language,
    )

    try:
        log = PaymentReminderLog.objects.select_related("invoice", "invoice__client", "company").get(pk=log_id)
    except PaymentReminderLog.DoesNotExist:
        logger.error("PaymentReminderLog %s does not exist, skipping send.", log_id)
        return {"status": "error", "message": f"PaymentReminderLog {log_id} not found."}

    if log.status != PaymentReminderLogStatus.PENDING:
        return {"status": "skipped", "reason": f"log status is {log.status}"}

    invoice = log.invoice
    client = invoice.client

    if (
        invoice.payment_status == InvoicePaymentStatus.PAID
        or invoice.exclude_from_payment_reminders
        or client.exclude_from_payment_reminders
    ):
        log.status = PaymentReminderLogStatus.CANCELLED
        log.save(update_fields=["status"])
        return {"status": "cancelled", "log_id": log_id}

    recipients = client.payment_reminder_recipients()
    if not recipients:
        log.status = PaymentReminderLogStatus.FAILED
        log.error_message = "Client has no email address on file."
        log.save(update_fields=["status", "error_message"])
        logger.warning("Cannot send payment reminder for invoice %s: client has no email.", invoice.pk)
        return {"status": "failed", "log_id": log_id, "reason": "no client email"}

    language = resolve_reminder_language(invoice)

    from invoices.services.payment_reminder_content import get_reminder_subject, get_reminder_template

    subject = get_reminder_subject(language, log.reminder_type, invoice.invoice_number)
    template_name = get_reminder_template(language)

    inline_images = []
    if log.company.logo:
        try:
            log.company.logo.open("rb")
            logo_bytes = log.company.logo.read()
            log.company.logo.close()
            ext = log.company.logo.name.rsplit(".", 1)[-1].lower()
            mimetype = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"
            inline_images.append(("company_logo", logo_bytes, mimetype))
        except Exception:
            logger.warning("Could not load logo for company %s, sending without it.", log.company.pk)

    attachments = []
    pdf_warning = None
    try:
        from core.services.pdf_service import PDFService
        from configurator.models import ExportTemplateKind
        from invoices.services.invoice_pdf_download import InvoicePdfDownloadService

        pdf_bytes = PDFService.render_object_pdf(
            invoice, ExportTemplateKind.SALES_INVOICE, company=invoice.company
        )
        if pdf_bytes:
            filename = InvoicePdfDownloadService.sales_invoice_pdf_filename(invoice)
            attachments.append((filename, pdf_bytes, "application/pdf"))
        else:
            pdf_warning = "No PDF template found for this invoice."
            logger.warning(
                "No PDF template found for invoice %s, sending reminder without attachment.",
                invoice.pk,
            )
    except Exception as exc:
        pdf_warning = f"PDF generation failed: {exc}"
        logger.exception(
            "Failed to render PDF for invoice %s, sending reminder without attachment.",
            invoice.pk,
        )

    with translation.override(language):
        body_html = render_to_string(
            template_name,
            {
                "client_name": client.name,
                "company_name": log.company.name,
                "company_email": log.company.email,
                "company_phone": log.company.phone,
                "invoice_number": invoice.invoice_number,
                "due_date": invoice.due_date,
                "total_amount": invoice.total_amount,
                "amount_remaining": invoice.amount_remaining,
                "reminder_type": log.reminder_type,
                "is_overdue": log.reminder_type == PaymentReminderType.OVERDUE,
                "has_logo": bool(inline_images),
                "has_attachment": bool(attachments),
            },
        )

    try:
        success = EmailService.send_email(
            to=recipients,
            subject=subject,
            body_html=body_html,
            attachments=attachments or None,
            inline_images=inline_images or None,
            related_object=invoice,
            company=log.company,
            email_document="payment_reminder",
        )
    except Exception as exc:
        logger.exception("Error sending payment reminder for invoice %s", invoice.pk)
        log.status = PaymentReminderLogStatus.FAILED
        log.error_message = str(exc)
        log.save(update_fields=["status", "error_message"])
        raise self.retry(exc=exc)

    if success:
        log.status = PaymentReminderLogStatus.SENT
        log.sent_at = timezone.now()
        if pdf_warning:
            log.error_message = f"Warning: sent without PDF attachment — {pdf_warning}"
            log.save(update_fields=["status", "sent_at", "error_message"])
        else:
            log.save(update_fields=["status", "sent_at"])
        return {"status": "sent", "log_id": log_id, "pdf_attached": pdf_warning is None}
    else:
        log.status = PaymentReminderLogStatus.FAILED
        log.error_message = "EmailService.send_email returned False."
        log.save(update_fields=["status", "error_message"])
        return {"status": "failed", "log_id": log_id}


@shared_task(
    name="invoices.tasks.send_consolidated_payment_reminder_email",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    rate_limit=PAYMENT_REMINDER_SEND_RATE_LIMIT,
)
def send_consolidated_payment_reminder_email(self, log_ids: list[int]):
    """
    Sends ONE email covering several invoices of the same client, sharing
    a batch_id across their PaymentReminderLog rows. Manual-only entry
    point (from SendPaymentReminderNowAdminView's consolidated form) --
    never called by process_payment_reminders -- so unlike the automatic
    path it does not treat a given invoice's own "Exclude From Payment
    Reminders" flag as a hard stop: the human already chose to include it
    on the checklist, so that flag is not re-checked here. Payment status
    and the client-level "Do Not Send Payment Reminders" flag are still
    re-validated, since those reflect facts/decisions the human either
    can't override (already paid) or didn't specifically opt out of here
    (client-wide preference).
    """
    from datetime import date

    from django.template.loader import render_to_string
    from django.utils import timezone, translation

    from invoices.models import (
        PaymentReminderLog,
        PaymentReminderLogStatus,
        PaymentReminderType,
        InvoicePaymentStatus,
        resolve_reminder_language,
    )
    from invoices.services.payment_reminder_content import (
        get_consolidated_reminder_subject,
        get_consolidated_reminder_template,
    )

    logs = list(
        PaymentReminderLog.objects.select_related("invoice", "invoice__client", "company")
        .filter(pk__in=log_ids, status=PaymentReminderLogStatus.PENDING)
    )
    if not logs:
        return {"status": "skipped", "reason": "no pending logs", "log_ids": log_ids}

    client = logs[0].invoice.client
    company = logs[0].company

    if client.exclude_from_payment_reminders:
        for log in logs:
            log.status = PaymentReminderLogStatus.CANCELLED
            log.save(update_fields=["status"])
        return {"status": "cancelled", "log_ids": log_ids, "reason": "client excluded"}

    recipients = client.payment_reminder_recipients()
    if not recipients:
        for log in logs:
            log.status = PaymentReminderLogStatus.FAILED
            log.error_message = "Client has no email address on file."
            log.save(update_fields=["status", "error_message"])
        return {"status": "failed", "log_ids": log_ids, "reason": "no client email"}

    valid_logs = []
    for log in logs:
        if log.invoice.payment_status == InvoicePaymentStatus.PAID:
            log.status = PaymentReminderLogStatus.CANCELLED
            log.save(update_fields=["status"])
            continue
        valid_logs.append(log)

    if not valid_logs:
        return {"status": "cancelled", "log_ids": log_ids, "reason": "all invoices already paid"}

    language = resolve_reminder_language(valid_logs[0].invoice)
    subject = get_consolidated_reminder_subject(language, len(valid_logs))
    template_name = get_consolidated_reminder_template(language)

    inline_images = []
    if company.logo:
        try:
            company.logo.open("rb")
            logo_bytes = company.logo.read()
            company.logo.close()
            ext = company.logo.name.rsplit(".", 1)[-1].lower()
            mimetype = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"
            inline_images.append(("company_logo", logo_bytes, mimetype))
        except Exception:
            logger.warning("Could not load logo for company %s, sending without it.", company.pk)

    invoice_rows = []
    attachments = []
    total_amount = None
    for log in valid_logs:
        invoice = log.invoice
        days_diff = (invoice.due_date - date.today()).days
        is_overdue = days_diff < 0

        invoice_rows.append({
            "invoice_number": invoice.invoice_number,
            "due_date": invoice.due_date,
            "is_overdue": is_overdue,
            "amount": str(invoice.amount_remaining),
        })
        total_amount = invoice.amount_remaining if total_amount is None else total_amount + invoice.amount_remaining

        try:
            from core.services.pdf_service import PDFService
            from configurator.models import ExportTemplateKind
            from invoices.services.invoice_pdf_download import InvoicePdfDownloadService

            pdf_bytes = PDFService.render_object_pdf(
                invoice, ExportTemplateKind.SALES_INVOICE, company=invoice.company
            )
            if pdf_bytes:
                filename = InvoicePdfDownloadService.sales_invoice_pdf_filename(invoice)
                attachments.append((filename, pdf_bytes, "application/pdf"))
        except Exception:
            logger.exception(
                "Failed to render PDF for invoice %s in consolidated reminder, skipping its attachment.",
                invoice.pk,
            )

    with translation.override(language):
        body_html = render_to_string(
            template_name,
            {
                "client_name": client.name,
                "company_name": company.name,
                "company_email": company.email,
                "company_phone": company.phone,
                "invoice_count": len(valid_logs),
                "invoices": invoice_rows,
                "total_amount": str(total_amount),
                "has_logo": bool(inline_images),
                "has_attachment": bool(attachments),
            },
        )

    try:
        success = EmailService.send_email(
            to=recipients,
            subject=subject,
            body_html=body_html,
            attachments=attachments or None,
            inline_images=inline_images or None,
            related_object=client,
            company=company,
            email_document="payment_reminder",
        )
    except Exception as exc:
        logger.exception("Error sending consolidated payment reminder for client %s", client.pk)
        for log in valid_logs:
            log.status = PaymentReminderLogStatus.FAILED
            log.error_message = str(exc)
            log.save(update_fields=["status", "error_message"])
        raise self.retry(exc=exc)

    if success:
        for log in valid_logs:
            log.status = PaymentReminderLogStatus.SENT
            log.sent_at = timezone.now()
            log.save(update_fields=["status", "sent_at"])
        return {"status": "sent", "log_ids": [log.id for log in valid_logs], "invoice_count": len(valid_logs)}
    else:
        for log in valid_logs:
            log.status = PaymentReminderLogStatus.FAILED
            log.error_message = "EmailService.send_email returned False."
            log.save(update_fields=["status", "error_message"])
        return {"status": "failed", "log_ids": [log.id for log in valid_logs]}

def _process_forecast_report_settings(settings_model, send_task, build_send_args):
    """
    Generic dispatcher body shared by every *ForecastEmailSettings model
    (CollectionsForecastEmailSettings, PaymentsForecastEmailSettings, ...):
    for each active row whose send_time falls inside the current 30-minute
    window and whose frequency_days has elapsed since last_run_date,
    queues send_task with whatever args build_send_args(settings_row)
    produces, then marks last_run_date as today.

    Returns (processed, queued) so the calling task can log/report totals
    per report type.
    """
    from datetime import datetime, timedelta

    from django.utils import timezone

    now = timezone.localtime()
    today = now.date()
    queued = 0
    processed = 0

    settings_qs = settings_model.objects.filter(is_active=True).select_related("company")

    for report_settings in settings_qs:
        if (
            report_settings.last_run_date
            and (today - report_settings.last_run_date).days < report_settings.frequency_days
        ):
            continue

        scheduled_dt = timezone.make_aware(datetime.combine(today, report_settings.send_time))
        if not (scheduled_dt <= now < scheduled_dt + timedelta(minutes=30)):
            continue

        send_task.delay(*build_send_args(report_settings))
        report_settings.last_run_date = today
        report_settings.save(update_fields=["last_run_date"])
        queued += 1
        processed += 1

    return processed, queued


@shared_task(name="invoices.tasks.process_forecast_reports")
def process_forecast_reports():
    """
    Celery-beat entry point for both the collections-forecast and
    payments-forecast report emails, scheduled every 30 minutes -- same
    cadence as process_payment_reminders. Both *ForecastEmailSettings
    models share the exact same shape (is_active, send_time,
    frequency_days, last_run_date), so a single generic dispatcher
    (_process_forecast_report_settings) decides whose turn it is today;
    only the actual send tasks differ (different email content/service).
    """
    from invoices.models import CollectionsForecastEmailSettings, PaymentsForecastEmailSettings

    collections_processed, collections_queued = _process_forecast_report_settings(
        CollectionsForecastEmailSettings,
        send_collections_forecast_report_email,
        lambda s: (s.company_id, s.days_ahead),
    )
    payments_processed, payments_queued = _process_forecast_report_settings(
        PaymentsForecastEmailSettings,
        send_payments_forecast_report_email,
        lambda s: (s.company_id, s.days_ahead, s.payment_methods or None),
    )

    logger.info(
        "Forecast report check complete: collections %d processed/%d queued, "
        "payments %d processed/%d queued.",
        collections_processed, collections_queued, payments_processed, payments_queued,
    )
    return {
        "collections_processed": collections_processed,
        "collections_queued": collections_queued,
        "payments_processed": payments_processed,
        "payments_queued": payments_queued,
    }


@shared_task(
    name="invoices.tasks.send_collections_forecast_report_email",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
)
def send_collections_forecast_report_email(self, company_id: int, days_ahead: int = 14):
    """
    Builds and sends one company's collections-forecast report email.
    Used both by process_forecast_reports (scheduled) and by
    the "Send now" button on the Collections Forecast calendar (manual) --
    same task either way, so scheduled and manual sends behave identically.

    Loads the company logo the same way send_payment_reminder_email does,
    so the internal report and the client-facing reminder look consistent.
    """
    from company.models import Company
    from invoices.models import CollectionsForecastEmailSettings
    from invoices.services.collections_forecast_service import CollectionsForecastService
    from invoices.services.collections_forecast_report_email_service import (
        CollectionsForecastReportEmailService,
    )

    try:
        company = Company.objects.get(pk=company_id)
    except Company.DoesNotExist:
        logger.error("Collections forecast report: company %s does not exist.", company_id)
        return {"status": "error", "message": f"Company {company_id} does not exist."}

    report_settings = CollectionsForecastEmailSettings.objects.filter(company=company).first()
    if report_settings:
        recipients = report_settings.report_recipients()
    else:
        recipients = [company.email.strip()] if company.email and company.email.strip() else []

    if not recipients:
        logger.warning(
            "Collections forecast report for company %s not sent: no recipients configured.",
            company.pk,
        )
        return {"status": "skipped", "reason": "no recipients"}

    inline_images = []
    if company.logo:
        try:
            company.logo.open("rb")
            logo_bytes = company.logo.read()
            company.logo.close()
            ext = company.logo.name.rsplit(".", 1)[-1].lower()
            mimetype = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"
            inline_images.append(("company_logo", logo_bytes, mimetype))
        except Exception:
            logger.warning("Could not load logo for company %s, sending without it.", company.pk)

    forecast = CollectionsForecastService.build_forecast(company, days_ahead)
    email = CollectionsForecastReportEmailService.build_email(
        company, forecast, recipients=recipients, has_logo=bool(inline_images)
    )

    success = EmailService.send_email(
        to=email.recipients,
        subject=email.subject,
        body_html=email.body_html,
        inline_images=inline_images or None,
        related_object=company,
        company=company,
        email_document="collections_forecast_report",
    )
    if not success:
        raise Exception(
            f"Failed to send collections forecast report for company {company.pk} to [{', '.join(recipients)}]"
        )

    return {
        "task": "send_collections_forecast_report_email",
        "status": "ok",
        "company_id": company.pk,
        "recipients": recipients,
    }


@shared_task(name="invoices.tasks.send_payments_forecast_report_email", bind=True, max_retries=2, default_retry_delay=60)
def send_payments_forecast_report_email(self, company_id: int, days_ahead: int, payment_methods: list | None = None):
    """
    Builds and sends one company's Payments Forecast report email. Used
    both by the scheduled beat job (process_forecast_reports)
    and the "Send Report by Email Now" button on the calendar itself, so
    both paths always produce the identical email for a given
    company/window/payment-method combination -- the button sends
    whatever is currently on screen, not necessarily the company's saved
    settings.

    Recipients: PaymentsForecastEmailSettings.report_emails if a settings
    row exists for the company, falling back to Company.email. If neither
    yields a recipient, the task is skipped rather than failed -- this is
    also the path a manual send takes for a company with no settings row
    configured yet, so it should degrade gracefully rather than error.
    """
    from company.models import Company

    from invoices.models import PaymentsForecastEmailSettings
    from invoices.services.payments_forecast_report_email_service import (
        PaymentsForecastReportEmailService,
    )
    from invoices.services.payments_forecast_service import PaymentsForecastService

    try:
        company = Company.objects.get(pk=company_id)
    except Company.DoesNotExist:
        logger.error("No company %s, skipping payments forecast report.", company_id)
        return {"status": "error", "message": f"No company {company_id}."}

    forecast_settings = PaymentsForecastEmailSettings.objects.filter(company=company).first()
    if forecast_settings:
        recipients = forecast_settings.report_recipients()
    else:
        recipients = [company.email.strip()] if getattr(company, "email", "") and company.email.strip() else []

    if not recipients:
        logger.warning(
            "Payments forecast report for company %s not sent: no report emails configured "
            "and the company has no email.",
            company_id,
        )
        return {"status": "skipped", "reason": "no recipients"}

    inline_images = []
    if company.logo:
        try:
            company.logo.open("rb")
            logo_bytes = company.logo.read()
            company.logo.close()
            ext = company.logo.name.rsplit(".", 1)[-1].lower()
            mimetype = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"
            inline_images.append(("company_logo", logo_bytes, mimetype))
        except Exception:
            logger.warning("Could not load logo for company %s, sending without it.", company.pk)

    forecast = PaymentsForecastService.build_forecast(company, days_ahead, payment_methods)
    email = PaymentsForecastReportEmailService.build_email(
        company, forecast, recipients=recipients, has_logo=bool(inline_images), payment_methods=payment_methods
    )

    success = EmailService.send_email(
        to=email.recipients,
        subject=email.subject,
        body_html=email.body_html,
        inline_images=inline_images or None,
        related_object=company,
        company=company,
        email_document="payments_forecast_report",
    )
    if not success:
        raise Exception(
            f"Failed to send payments forecast report for company {company.pk} to [{', '.join(recipients)}]"
        )

    return {
        "task": "send_payments_forecast_report_email",
        "status": "ok",
        "company_id": company.pk,
        "days_ahead": days_ahead,
        "payment_methods": payment_methods,
        "recipients": recipients,
    }