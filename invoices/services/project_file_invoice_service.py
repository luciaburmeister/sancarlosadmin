import logging
from decimal import Decimal
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext_lazy as _

from invoices.models import SalesInvoice, SalesInvoiceItem, SalesInvoiceStatus, AppliedTax
from invoices.services.sales_invoice_payment import resolve_sales_invoice_payment_method
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
        - total_invoiceable: sum of accepted quotes' total_price + direct configurations' total_price
        - total_invoiced: sum of existing SalesInvoices linked to this ProjectFile
        - remaining: total_invoiceable - total_invoiced
        - quotes: list of accepted quotes
        - direct_configurations: list of direct (non-quote) configurations
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

        # Direct configurations (shop/work-order flow, NOT already counted via a quote)
        from configurator.models import Configuration, ConfigurationStatus

        direct_configs = Configuration.objects.filter(
            project_file=project_file,
            quote__isnull=True,
            status__in=[
                ConfigurationStatus.SHOP,
                ConfigurationStatus.COMPLETED,
                ConfigurationStatus.CONVERTED,
                ConfigurationStatus.PRODUCTION_DONE,
            ],
        )

        direct_configs_data = []
        for config in direct_configs:
            sub = Decimal(str(config.subtotal_price.amount))
            tot = Decimal(str(config.total_price.amount))
            total_invoiceable += tot
            total_invoiceable_subtotal += sub
            direct_configs_data.append({
                'id': config.id,
                'name': config.name,
                'subtotal': sub,
                'total': tot,
                'status': config.get_status_display(),
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
            'direct_configurations': direct_configs_data,
            'invoices': invoices_data,
            'project_file': project_file,
            'percentage_invoiced': (
                (total_invoiced / total_invoiceable * Decimal("100")).quantize(Decimal("0.01"))
                if total_invoiceable > 0 else Decimal("0.00")
            ),
        }

    @classmethod
    def compute_invoice_line_ratio(cls, summary, invoice_mode, percentage, custom_amount=None):
        """
        Uniform ratio applied to each configuration subtotal when building invoice lines.

        Percentage mode bills a fraction of the *remaining* subtotal (not the full expediente),
        matching the mobile/admin UX ("100% del pendiente").
        Custom mode bills a fixed subtotal amount spread proportionally across line items.
        """
        total_sub = summary["total_invoiceable_subtotal"]
        remaining_sub = summary["remaining_subtotal"]

        if total_sub <= 0:
            return Decimal("0")

        if invoice_mode == "percentage":
            pct = Decimal(str(percentage)) / Decimal("100")
            if remaining_sub <= 0:
                return Decimal("0")
            target_sub = remaining_sub * pct
            return target_sub / total_sub

        custom = Decimal(str(custom_amount))
        if custom <= 0 or remaining_sub <= 0:
            return Decimal("0")
        # Spread [custom] across all line items proportionally (same as percentage).
        # Must divide by total_sub, not remaining_sub — otherwise invoicing the full
        # pending amount yields ratio=1 and re-bills the entire expediente.
        return min(custom, remaining_sub) / total_sub

    @classmethod
    def get_aggregated_quote_taxes(cls, project_file):
        """
        Retrieves a deduplicated list of AppliedTax entries from all accepted quotes'
        configurations and direct (non-quote) configurations for this ProjectFile.
        Falls back to Quote-level AppliedTax if no configuration taxes are found.
        Returns a list of dicts with tax_type and selected_percentage.
        Deduplicates by (behavior, selected_percentage) to avoid applying
        functionally identical taxes from different TaxType records.
        """
        from configurator.models import Configuration, ConfigurationStatus

        accepted_quotes = Quote.objects.filter(
            project_file=project_file,
            status=QuoteStatus.ACCEPTED,
        )

        # Get taxes from Configurations linked to accepted quotes
        config_ct = ContentType.objects.get_for_model(Configuration)
        config_ids = list(
            Configuration.objects.filter(
                quote__in=accepted_quotes
            ).values_list('id', flat=True)
        )

        # Also include direct configurations (shop/work-order flow)
        direct_config_ids = list(
            Configuration.objects.filter(
                project_file=project_file,
                quote__isnull=True,
                status__in=[
                    ConfigurationStatus.SHOP,
                    ConfigurationStatus.COMPLETED,
                    ConfigurationStatus.CONVERTED,
                    ConfigurationStatus.PRODUCTION_DONE,
                ],
            ).values_list('id', flat=True)
        )

        all_config_ids = config_ids + direct_config_ids

        taxes = AppliedTax.objects.filter(
            content_type=config_ct,
            object_id__in=all_config_ids,
        ).select_related('tax_type').order_by('order', 'tax_type')

        # Deduplicate by (behavior, selected_percentage) to collapse
        # functionally identical taxes from different TaxType records
        # (e.g. "IVA" and "IVA 21%" both at 21% additive).
        seen = set()
        unique_taxes = []
        for tax in taxes:
            if tax.selected_percentage == 0:
                continue  # Skip 0% taxes — they have no effect
            key = (tax.tax_type.behavior, tax.selected_percentage)
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
            if tax.selected_percentage == 0:
                continue
            key = (tax.tax_type.behavior, tax.selected_percentage)
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
        total_tax=Decimal("0.00"),
        notes="",
        payment_method=None,
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
            total_tax: Pre-computed total tax from per-config taxes (scaled by invoicing %)
            notes: Optional notes
            payment_method: Expected payment method (defaults to client.payment_method)

        Returns:
            SalesInvoice instance

        Raises:
            ValidationError if the invoice amount would exceed the remaining invoiceable amount
        """
        summary = cls.get_invoiceable_summary(project_file)

        if summary['total_invoiceable'] <= 0:
            raise ValidationError(
                _("This project file has no accepted quotes or direct configurations. Cannot create an invoice.")
            )

        resolved_payment_method = resolve_sales_invoice_payment_method(
            client=client,
            explicit=payment_method,
        )

        # Create the invoice
        invoice = SalesInvoice.objects.create(
            company=company,
            status=SalesInvoiceStatus.PROFORMA,
            invoice_number=None,
            date_issued=date_issued,
            due_date=due_date,
            client=client,
            project_file=project_file,
            notes=notes,
            payment_method=resolved_payment_method,
        )

        # Create line items (pre-tax subtotals)
        subtotal = Decimal("0.00")
        for item_data in items_data:
            item = SalesInvoiceItem.objects.create(
                invoice=invoice,
                description=item_data['description'],
                quote_name=item_data.get('quote_name', ''),
                quantity=item_data.get('quantity', Decimal("1")),
                unit_price=item_data['unit_price'],
                discount_percentage=item_data.get('discount_percentage', Decimal("0.00")),
                source_id=item_data.get('source_id', ''),
            )
            subtotal += item.line_total.amount

        # Create AppliedTax records from per-config tax profiles.
        # The total_tax is pre-computed from per-config taxes (scaled by the
        # invoicing percentage). Distribute across tax entries proportionally.
        ct = ContentType.objects.get_for_model(invoice)
        tax_entries = cls._compute_per_config_taxes(project_file)
        full_tax = sum(e['computed_amount'] for e in tax_entries)

        for order, entry in enumerate(tax_entries):
            if full_tax > 0:
                proportion = entry['computed_amount'] / full_tax
                scaled_amount = (total_tax * proportion).quantize(Decimal("0.01"))
            else:
                scaled_amount = Decimal("0.00")
            AppliedTax.objects.create(
                content_type=ct,
                object_id=invoice.id,
                tax_type=entry['tax_type'],
                selected_percentage=entry['selected_percentage'],
                base_amount=subtotal,
                calculated_amount=scaled_amount,
                order=order,
            )

        invoice.subtotal_amount = subtotal
        invoice.tax_amount = total_tax
        invoice.total_amount = subtotal + total_tax
        invoice.save(update_fields=['subtotal_amount', 'tax_amount', 'total_amount'])

        # Validate: the new total should not exceed the remaining invoiceable amount.
        # Use a tolerance because per-config tax rounding may cause small differences.
        new_summary = cls.get_invoiceable_summary(project_file)
        tolerance = summary['total_invoiceable'] * Decimal("0.001")  # 2% tolerance

        if new_summary['remaining'] < -tolerance:
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
            "Amount: %s (subtotal: %s, tax: %s). Remaining: %s",
            invoice.id, project_file.reference,
            invoice.total_amount, invoice.subtotal_amount,
            invoice.tax_amount, new_summary['remaining']
        )

        return invoice

    @classmethod
    def _compute_per_config_taxes(cls, project_file):
        """
        Computes per-config tax amounts and groups them by (behavior, percentage).
        Returns a list of dicts with tax_type, selected_percentage, and computed_amount.
        Each entry's computed_amount is the sum of (total_price - subtotal_price) for
        all configs sharing the same tax profile.
        """
        from configurator.models import Configuration, ConfigurationStatus

        config_ct = ContentType.objects.get_for_model(Configuration)

        # Gather all configs (from quotes + direct)
        accepted_quotes = Quote.objects.filter(
            project_file=project_file,
            status=QuoteStatus.ACCEPTED,
        )
        quote_config_ids = list(
            Configuration.objects.filter(quote__in=accepted_quotes).values_list('id', flat=True)
        )
        direct_config_ids = list(
            Configuration.objects.filter(
                project_file=project_file,
                quote__isnull=True,
                status__in=[
                    ConfigurationStatus.SHOP,
                    ConfigurationStatus.COMPLETED,
                    ConfigurationStatus.CONVERTED,
                    ConfigurationStatus.PRODUCTION_DONE,
                ],
            ).values_list('id', flat=True)
        )

        all_config_ids = quote_config_ids + direct_config_ids
        all_configs = Configuration.objects.filter(id__in=all_config_ids)

        # Build a map: config_id -> its AppliedTax info (first non-zero tax)
        config_taxes = {}
        taxes_qs = AppliedTax.objects.filter(
            content_type=config_ct,
            object_id__in=all_config_ids,
        ).select_related('tax_type').order_by('order')

        for tax in taxes_qs:
            if tax.object_id not in config_taxes and tax.selected_percentage > 0:
                config_taxes[tax.object_id] = tax

        # Group per-config tax amounts by (behavior, percentage)
        grouped = {}  # (behavior, percentage) -> {tax_type, amount}
        for config in all_configs:
            config_tax_diff = config.total_price.amount - config.subtotal_price.amount
            if config_tax_diff <= 0:
                continue

            tax = config_taxes.get(config.id)
            if not tax:
                continue

            key = (tax.tax_type.behavior, tax.selected_percentage)
            if key not in grouped:
                grouped[key] = {
                    'tax_type': tax.tax_type,
                    'selected_percentage': tax.selected_percentage,
                    'computed_amount': Decimal("0.00"),
                }
            grouped[key]['computed_amount'] += config_tax_diff

        return list(grouped.values())


