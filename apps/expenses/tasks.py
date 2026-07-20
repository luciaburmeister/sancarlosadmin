from celery import shared_task
from django.conf import settings
from django.urls import reverse
import logging
import tempfile
import os
import shutil

from apps.expenses.models import ExpenseTicket
from apps.expenses.services.ocr_services.gemini_ocr import GeminiExpenseProcessor
from apps.expenses.services.expense_ticket_service import ExpenseTicketService
from core.services.email_service import EmailService

logger = logging.getLogger(__name__)


@shared_task(name="expenses.tasks.process_expense_ticket")
def process_expense_ticket(ticket_id: int):
    """
    Background task to run OCR on a manually uploaded ExpenseTicket.
    Triggered after the ticket is saved with a photo.
    """
    logger.info(f"Processing expense ticket {ticket_id}")

    try:
        ticket = ExpenseTicket.objects.get(pk=ticket_id)
    except ExpenseTicket.DoesNotExist:
        logger.error(f"ExpenseTicket {ticket_id} does not exist.")
        return {"task": "process_expense_ticket", "ticket_id": ticket_id, "status": "error", "detail": "not found"}

    if not ticket.photo:
        logger.error(f"ExpenseTicket {ticket_id} has no photo attached.")
        return {"task": "process_expense_ticket", "ticket_id": ticket_id, "status": "error", "detail": "no photo"}

    api_key = settings.GEMINI_API_KEY
    if not api_key:
        logger.error("GEMINI_API_KEY not configured.")
        ticket.ocr_error = "GEMINI_API_KEY not configured."
        ticket.save(update_fields=["ocr_error"])
        return {"task": "process_expense_ticket", "ticket_id": ticket_id, "status": "error", "detail": "missing API key"}

    ocr_processor = GeminiExpenseProcessor(api_key=api_key)
    service = ExpenseTicketService(ocr_processor=ocr_processor)

    temp_file_path = None
    try:
        try:
            file_path = ticket.photo.path
        except NotImplementedError:
            suffix = os.path.splitext(ticket.photo.name)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                with ticket.photo.open("rb") as f:
                    shutil.copyfileobj(f, tmp)
                temp_file_path = tmp.name
                file_path = temp_file_path

        result = service.process_existing_ticket(ticket, file_path=file_path)

        if result.success:
            logger.info(f"Successfully processed ExpenseTicket {ticket_id}")
            return {"task": "process_expense_ticket", "ticket_id": ticket_id, "status": "ok"}
        else:
            if getattr(result, 'is_duplicate', False):
                duplicate_of = getattr(result, 'duplicate_of', None)
                logger.warning(f"Duplicate ExpenseTicket {ticket.id}: {result.messages}")

                if ticket.submitted_by and ticket.submitted_by.email and duplicate_of:
                    original_link = f"{settings.BACKEND_URL.rstrip('/')}{reverse('admin:expenses_expenseticket_change', args=[duplicate_of.pk])}"
                    EmailService.send_email(
                        to=[ticket.submitted_by.email],
                        subject=f"Duplicate receipt detected: {ticket.photo.name}",
                        body_html=(
                            f"<p>The receipt you uploaded ({ticket.photo.name}) is a duplicate of a "
                            f"receipt that already exists in the system and has been deleted.</p>"
                            f"<p><a href=\"{original_link}\">View the original receipt</a></p>"
                        ),
                        related_object=duplicate_of,
                        company=duplicate_of.company,
                    )

                ticket_id_for_log = ticket.id
                ticket.delete()
                return {"task": "process_expense_ticket", "ticket_id": ticket_id_for_log, "status": "duplicate_deleted"}

            logger.error(f"Failed to process ExpenseTicket {ticket_id}: {result.messages}")
            return {"task": "process_expense_ticket", "ticket_id": ticket_id, "status": "error", "detail": str(result.messages)}

    except Exception as e:
        logger.error(f"Exception processing ExpenseTicket {ticket_id}: {str(e)}")
        raise

    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            os.remove(temp_file_path)
