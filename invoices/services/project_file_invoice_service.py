import logging
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext_lazy as _

from invoices.models import SalesInvoice, SalesInvoiceItem, SalesInvoiceStatus, AppliedTax
from management.models import ProjectFile
from sales.models import Quote, QuoteStatus

logger = logging.getLogger(__name__)


class ProjectFileInvoiceService:
    """
    Service for creating Sales Invoices from ProjectFiles (Expedientes).
    Supports partial invoicing with validation to prevent over-invoicing.
    """

    @classmethod
    def get_invoiceable_summary(cls, project_file):
        """
        Returns a dict with the invoicing status for a ProjectFile:
        - total_invoiceable: sum of accepted quotes' total_price
        - total_invoiced: sum of existing SalesInvoices linked to this ProjectFile
        - remaining: total_invoiceable - total_invoiced
        - quotes: list of accepted quotes
        - invoices: list of existing invoices
        """
        accepted_quotes = Quote.objects.filter(
            project_file=project_file,
            status=QuoteStatus.ACCEPTED,
        ).order_by('id')

        total_invoiceable = Decimal("0.00")
        total_invoiceable_subtotal = Decimal("0.00")
        quotes_data = []

        for quote in accepted_quotes:
            total_amt = Decimal(str(quote.total_price.amount))
            subtotal_amt = Decimal(str(quote.subtotal_price.amount))
            tax_amt = Decimal(str(quote.tax_amount.amount))
            total_invoiceable += total_amt
            total_invoiceable_subtotal += subtotal_amt
            quotes_data.append({
                'id': quote.id,
                'project_name': quote.project_name,
                'subtotal': subtotal_amt,
                'tax_amount': tax_amt,
                'total': total_amt,
                'tax_breakdown': quote.tax_breakdown or [],
                'quote': quote,
            })

        # Sum existing invoices linked to this project file
        existing_invoices = SalesInvoice.objects.filter(
            project_file=project_file,
        ).order_by('-date_issued')

        total_invoiced = Decimal("0.00")
        total_invoiced_subtotal = Decimal("0.00")
        invoices_data = []

        for invoice in existing_invoices:
            inv_total = Decimal(str(invoice.total_amount.amount))
            inv_subtotal = Decimal(str(invoice.subtotal_amount.amount))
            total_invoiced += inv_total
            total_invoiced_subtotal += inv_subtotal
            invoices_data.append({
                'id': invoice.id,
                'invoice_number': invoice.invoice_number or f"Pro Forma #{invoice.id}",
                'status': invoice.get_status_display(),
                'subtotal': inv_subtotal,
                'total': inv_total,
                'date_issued': invoice.date_issued,
            })

        remaining = total_invoiceable - total_invoiced
        remaining_subtotal = total_invoiceable_subtotal - total_invoiced_subtotal

        return {
            'total_invoiceable': total_invoiceable,
            'total_invoiceable_subtotal': total_invoiceable_subtotal,
            'total_invoiced': total_invoiced,
            'total_invoiced_subtotal': total_invoiced_subtotal,
            'remaining': remaining,
            'remaining_subtotal': remaining_subtotal,
            'quotes': quotes_data,
            'invoices': invoices_data,
            'project_file': project_file,
            'percentage_invoiced': (
                (total_invoiced / total_invoiceable * Decimal("100")).quantize(Decimal("0.01"))
                if total_invoiceable > 0 else Decimal("0.00")
            ),
        }

    @classmethod
    def get_aggregated_quote_taxes(cls, project_file):
        """
        Retrieves a deduplicated list of AppliedTax entries from all accepted quotes'
        configurations for this ProjectFile. Falls back to Quote-level AppliedTax
        if no configuration taxes are found.
        Returns a list of dicts with tax_type and selected_percentage.
        """
        from configurator.models import Configuration

        accepted_quotes = Quote.objects.filter(
            project_file=project_file,
            status=QuoteStatus.ACCEPTED,
        )

        # First try: get taxes from Configurations linked to accepted quotes
        config_ct = ContentType.objects.get_for_model(Configuration)
        config_ids = list(
            Configuration.objects.filter(
                quote__in=accepted_quotes
            ).values_list('id', flat=True)
        )

        taxes = AppliedTax.objects.filter(
            content_type=config_ct,
            object_id__in=config_ids,
        ).select_related('tax_type').order_by('order', 'tax_type')

        # Deduplicate by (tax_type_id, selected_percentage)
        seen = set()
        unique_taxes = []
        for tax in taxes:
            key = (tax.tax_type_id, tax.selected_percentage)
            if key not in seen:
                seen.add(key)
                unique_taxes.append({
                    'tax_type': tax.tax_type,
                    'tax_type_id': tax.tax_type_id,
                    'selected_percentage': tax.selected_percentage,
                    'order': tax.order,
                })

        if unique_taxes:
            return unique_taxes

        # Fallback: check Quote-level AppliedTax
        quote_ct = ContentType.objects.get_for_model(Quote)
        quote_ids = list(accepted_quotes.values_list('id', flat=True))

        taxes = AppliedTax.objects.filter(
            content_type=quote_ct,
            object_id__in=quote_ids,
        ).select_related('tax_type').order_by('order', 'tax_type')

        for tax in taxes:
            key = (tax.tax_type_id, tax.selected_percentage)
            if key not in seen:
                seen.add(key)
                unique_taxes.append({
                    'tax_type': tax.tax_type,
                    'tax_type_id': tax.tax_type_id,
                    'selected_percentage': tax.selected_percentage,
                    'order': tax.order,
                })

        return unique_taxes

    @classmethod
    @transaction.atomic
    def create_invoice_from_project_file(
        cls,
        project_file,
        company,
        client,
        date_issued,
        due_date,
        items_data,
        taxes_data,
        notes="",
    ):
        """
        Creates a SalesInvoice from a ProjectFile with validation.

        Args:
            project_file: The ProjectFile to invoice
            company: Company for the invoice
            client: Client for the invoice
            date_issued: Invoice date
            due_date: Payment due date
            items_data: List of dicts with description, quantity, unit_price, discount_percentage
            taxes_data: List of dicts with tax_type, selected_percentage
            notes: Optional notes

        Returns:
            SalesInvoice instance

        Raises:
            ValidationError if the invoice amount would exceed the remaining invoiceable amount
        """
        summary = cls.get_invoiceable_summary(project_file)

        if summary['total_invoiceable'] <= 0:
            raise ValidationError(
                _("This project file has no accepted quotes. Cannot create an invoice.")
            )

        # Create the invoice (amounts will be calculated after items + taxes)
        invoice = SalesInvoice.objects.create(
            company=company,
            status=SalesInvoiceStatus.PROFORMA,
            invoice_number=None,
            date_issued=date_issued,
            due_date=due_date,
            client=client,
            project_file=project_file,
            notes=notes,
        )

        # Create line items
        subtotal = Decimal("0.00")
        for item_data in items_data:
            item = SalesInvoiceItem.objects.create(
                invoice=invoice,
                description=item_data['description'],
                quantity=item_data.get('quantity', Decimal("1")),
                unit_price=item_data['unit_price'],
                discount_percentage=item_data.get('discount_percentage', Decimal("0.00")),
            )
            subtotal += item.line_total.amount

        invoice.subtotal_amount = subtotal
        invoice.save(update_fields=['subtotal_amount'])

        # Create taxes
        ct = ContentType.objects.get_for_model(invoice)
        for order, tax_data in enumerate(taxes_data):
            AppliedTax.objects.create(
                content_type=ct,
                object_id=invoice.id,
                tax_type=tax_data['tax_type'],
                selected_percentage=tax_data['selected_percentage'],
                order=order,
            )

        # Recalculate totals (this computes tax amounts)
        invoice.recalculate_totals()

        # Validate: the new total should not exceed the remaining invoiceable amount
        # Re-fetch summary to include this new invoice
        new_summary = cls.get_invoiceable_summary(project_file)

        if new_summary['remaining'] < Decimal("-0.01"):  # small tolerance
            # Rollback by raising inside the transaction
            raise ValidationError(
                _("This invoice would exceed the invoiceable amount. "
                  "Total invoiceable: %(invoiceable)s, "
                  "Already invoiced (including this): %(invoiced)s, "
                  "Over by: %(over)s") % {
                    'invoiceable': summary['total_invoiceable'],
                    'invoiced': new_summary['total_invoiced'],
                    'over': abs(new_summary['remaining']),
                }
            )

        logger.info(
            "Created SalesInvoice #%s from ProjectFile %s. "
            "Amount: %s. Remaining: %s",
            invoice.id, project_file.reference,
            invoice.total_amount, new_summary['remaining']
        )

        return invoice
