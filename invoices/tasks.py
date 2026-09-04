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
    """
    from datetime import datetime, timedelta

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

        invoices = (
            SalesInvoice.objects.filter(
                company=company,
                status=SalesInvoiceStatus.FINAL,
                due_date__isnull=False,
                exclude_from_payment_reminders=False,
            )
            .exclude(payment_status=InvoicePaymentStatus.PAID)
            .exclude(client__exclude_from_payment_reminders=True)
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

    logger.info(
        "Payment reminder check complete: %d companies processed, %d reminder(s) queued.",
        processed_companies, queued,
    )
    return {"companies_processed": processed_companies, "reminders_queued": queued}

@shared_task(name="invoices.tasks.send_payment_reminder_email", bind=True, max_retries=2, default_retry_delay=60)
def send_payment_reminder_email(self, log_id: int):
    """
    Sends the actual reminder email for a single PaymentReminderLog and
    updates its status. Re-checks the invoice right before sending so a
    payment or exclusion applied after the log was queued still stops it.
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

    if not client.email:
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
            to=[client.email],
            subject=subject,
            body_html=body_html,
            attachments=attachments or None,
            inline_images=inline_images or None,
            related_object=invoice,
            company=log.company,
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


@shared_task(name="invoices.tasks.send_consolidated_payment_reminder_email", bind=True, max_retries=2, default_retry_delay=60)
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

    if not client.email:
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
            to=[client.email],
            subject=subject,
            body_html=body_html,
            attachments=attachments or None,
            inline_images=inline_images or None,
            related_object=client,
            company=company,
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