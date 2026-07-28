"""
Orchestrates OCR processing for a single ExpenseTicket.
Mirrors the invoice module's process_existing_invoice pattern:
the ticket already exists in the DB (photo saved), this service
runs OCR and writes the extracted fields back to it.
"""

from datetime import datetime
from typing import Optional
from django.db import transaction
from djmoney.money import Money

from apps.expenses.models import ExpenseTicket
from apps.expenses.services.ocr_services.ocr_interface import TicketOcrInterface
from apps.expenses.services.ocr_services import GeminiJSONParseError
from core.services.provider_service import get_or_create_provider


class TicketProcessingResult:
    def __init__(self, ticket: Optional[ExpenseTicket], success: bool, messages: list, is_duplicate: bool = False, duplicate_of: Optional[ExpenseTicket] = None):
        self.ticket = ticket
        self.success = success
        self.messages = messages
        self.is_duplicate = is_duplicate
        self.duplicate_of = duplicate_of  # The existing ticket this one conflicts with, if any


class ExpenseTicketService:

    def __init__(self, ocr_processor: TicketOcrInterface):
        self.ocr_processor = ocr_processor

    def process_existing_ticket(self, ticket: ExpenseTicket, file_path: Optional[str] = None) -> TicketProcessingResult:
        """
        Run OCR on an already-saved ExpenseTicket and write extracted data back to it.
        Does NOT re-save the photo — only updates the data fields.
        """
        messages = []
        try:
            ticket.ocr_processing = True
            ticket.ocr_processed = False
            ticket.ocr_error = None
            ticket.save(update_fields=["ocr_processing", "ocr_processed", "ocr_error"])

            if not ticket.photo:
                raise ValueError("No photo attached to ticket.")

            if file_path is None:
                try:
                    file_path = ticket.photo.path
                except Exception:
                    raise ValueError("Ticket photo has no local path. Provide file_path from the task.")

            extracted = self.ocr_processor.extract_data(file_path)
            messages.append("OCR completed.")

            # Check for conflict with ANOTHER ticket (not itself), scoped to the same
            # company. Identity is vendor + date + amount, plus ticket_number when the
            # receipt has one — vendor/date/amount alone can coincide for two genuinely
            # separate purchases (e.g. two toll charges from the same vendor on the same
            # day), so a differing ticket_number rules out a conflict even if the rest match.
            purchase_date = self._parse_date(extracted.date_of_purchase)
            if extracted.amount is not None and purchase_date is not None:
                vendor_filter = (
                    {"vendor_tax_id": extracted.vendor_tax_id}
                    if extracted.vendor_tax_id
                    else {"vendor_name": extracted.vendor_name}
                )
                if vendor_filter.get("vendor_tax_id") or vendor_filter.get("vendor_name"):
                    conflict_filter = {
                        "company": ticket.company,
                        "date_of_purchase": purchase_date,
                        "original_amount": Money(extracted.amount, extracted.currency or "EUR"),
                        **vendor_filter,
                    }
                    if extracted.ticket_number:
                        conflict_filter["ticket_number"] = extracted.ticket_number

                    conflict = ExpenseTicket.objects.filter(**conflict_filter).exclude(pk=ticket.pk).first()
                    if conflict:
                        return TicketProcessingResult(
                            ticket=ticket,
                            success=False,
                            messages=messages + [f"Conflict detected: another ticket ({conflict.id}) already has the same vendor, date, amount{', and ticket number' if extracted.ticket_number else ''} for this company."],
                            is_duplicate=True,
                            duplicate_of=conflict,
                        )

            with transaction.atomic():
                ticket.vendor_name    = extracted.vendor_name or ticket.vendor_name
                ticket.vendor_tax_id  = extracted.vendor_tax_id or ticket.vendor_tax_id
                ticket.vendor_address = extracted.vendor_address or ticket.vendor_address
                ticket.vendor_city    = extracted.vendor_city or ticket.vendor_city
                ticket.vendor_country = extracted.vendor_country or ticket.vendor_country
                ticket.date_of_purchase = self._parse_date(extracted.date_of_purchase) or ticket.date_of_purchase
                ticket.ticket_number = extracted.ticket_number or ticket.ticket_number

                # Auto-link the ticket to a real Provider record, matched by tax ID
                # and scoped to the ticket's company. Same lookup/create logic the
                # invoices module uses, so a vendor is represented identically
                # whether it comes in through an invoice or an expense ticket.
                matched_provider = get_or_create_provider(
                    ticket.vendor_tax_id,
                    ticket.vendor_name,
                    ticket.company,
                )
                if not matched_provider and not ticket.provider:
                    messages.append("No tax ID extracted — provider not linked, needs manual review.")
                ticket.provider = matched_provider or ticket.provider

                if extracted.amount is not None:
                    ticket.original_amount = extracted.amount
                    ticket.original_amount_currency = extracted.currency or "EUR"

                ticket.ocr_raw_response  = extracted.raw_response
                ticket.ocr_processing    = False
                ticket.ocr_processed     = True
                ticket.ocr_error         = None
                ticket.needs_manual_review = True

                ticket.save(update_fields=[
                    "vendor_name", "vendor_tax_id", "vendor_address", "vendor_city",
                    "vendor_country", "date_of_purchase", "ticket_number", "provider",
                    "original_amount", "original_amount_currency",
                    "ocr_raw_response", "ocr_processing", "ocr_processed",
                    "ocr_error", "needs_manual_review",
                ])

                try:
                    ticket.apply_exchange_rate("EUR")
                    messages.append("Exchange rate applied.")
                except Exception as fx_error:
                    ticket.needs_manual_review = True
                    ticket.ocr_error = f"Currency conversion unavailable ({ticket.original_amount_currency}): {fx_error}"
                    ticket.save(update_fields=["needs_manual_review", "ocr_error"])
                    messages.append(f"Exchange rate not available for {ticket.original_amount_currency}, flagged for manual review.")

            return TicketProcessingResult(ticket=ticket, success=True, messages=messages)

        except Exception as e:
            messages.append(f"Processing failed: {str(e)}")
            ticket.ocr_processing = False
            ticket.ocr_processed = False
            ticket.ocr_error = str(e)
            if isinstance(e, GeminiJSONParseError):
                ticket.ocr_raw_response = e.raw_text
            ticket.needs_manual_review = True
            ticket.save(update_fields=["ocr_processing", "ocr_processed", "ocr_error", "ocr_raw_response", "needs_manual_review"])
            raise

    def _parse_date(self, date_str: Optional[str]):
        if not date_str:
            return None
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None
