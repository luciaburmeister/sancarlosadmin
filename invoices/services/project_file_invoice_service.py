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
                'units': config.units,
                'status': config.get_status_display(),
            })

        # Work entries (billable work linked to this project file)
        from management.models import WorkEntry

        work_entries = WorkEntry.objects.filter(
            project_file=project_file,
        ).select_related('entry_type')

        work_entries_data = []
        for entry in work_entries:
            entry_total = Decimal(str(entry.total_price.amount))
            total_invoiceable += entry_total
            total_invoiceable_subtotal += entry_total  # work entries have no separate subtotal/tax split at this level
            work_entries_data.append({
                'id': entry.id,
                'description': entry.description,
                'quantity': entry.quantity,
                'unit_price': Decimal(str(entry.unit_price.amount)),
                'total': entry_total,
                'entry_type': entry.entry_type.name,
                'date': entry.date,
            })

        # Sum existing invoices linked to this project file
        # Source 1: single-PF invoices (FK on invoice)
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

        # Source 2: bulk invoices (project_file FK on individual items)
        bulk_items = SalesInvoiceItem.objects.filter(
            project_file=project_file,
            invoice__project_file__isnull=True,  # only bulk invoices
        ).select_related("invoice")

        seen_bulk_invoices = set()
        for item in bulk_items:
            total_invoiced_subtotal += Decimal(str(item.line_total.amount))
            # Count total for each bulk invoice only once
            if item.invoice_id not in seen_bulk_invoices:
                seen_bulk_invoices.add(item.invoice_id)
                # For bulk invoices, add the full invoice to the list
                inv = item.invoice
                invoices_data.append({
                    'id': inv.id,
                    'invoice_number': inv.invoice_number or f"Pro Forma #{inv.id} (bulk)",
                    'status': inv.get_status_display(),
                    'subtotal': Decimal("0"),  # per-item, not per-invoice
                    'total': Decimal("0"),
                    'date_issued': inv.date_issued,
                    'is_bulk': True,
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
            'work_entries': work_entries_data,
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
        notes="",
        payment_method=None,
    ):
        """
        Creates a SalesInvoice from a ProjectFile with validation.

        Taxes are computed per line item from each source's own AppliedTax
        records (Configuration or QuoteInventoryItem), supporting mixed rates.

        Args:
            project_file: The ProjectFile to invoice
            company: Company for the invoice
            client: Client for the invoice
            date_issued: Invoice date
            due_date: Payment due date
            items_data: List of dicts with description, quantity, unit_price,
                        discount_percentage, and source_id
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

        # Create line items and compute per-line taxes from each source's
        # own AppliedTax records. This handles mixed tax rates correctly
        # (e.g. configs at 21% IVA, inventory items at 10% reduced rate).
        subtotal = Decimal("0.00")
        # Aggregation: (tax_type_id, behavior, percentage) → {tax_type, base, calc}
        tax_agg: dict[tuple, dict] = {}

        from configurator.models import Configuration
        from sales.models import QuoteInventoryItem

        config_ct = ContentType.objects.get_for_model(Configuration)
        inv_item_ct = ContentType.objects.get_for_model(QuoteInventoryItem)

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
            line_total = item.line_total.amount
            subtotal += line_total

            # Resolve the source's taxes from its source_id
            source_id = item_data.get('source_id', '')
            source_taxes = []

            if source_id.startswith("config_") or source_id.startswith("direct_"):
                obj_id = source_id.split("_", 1)[1]
                source_taxes = list(
                    AppliedTax.objects.filter(
                        content_type=config_ct, object_id=obj_id
                    ).select_related("tax_type")
                )
            elif source_id.startswith("inv_item_"):
                obj_id = source_id.split("_", 2)[2]
                source_taxes = list(
                    AppliedTax.objects.filter(
                        content_type=inv_item_ct, object_id=obj_id
                    ).select_related("tax_type")
                )
            # quote_item_ lines have no taxes

            for tax in source_taxes:
                if tax.selected_percentage <= 0:
                    continue
                key = (tax.tax_type_id, tax.tax_type.behavior, tax.selected_percentage)
                rate = tax.selected_percentage / Decimal("100")
                calc = (line_total * rate).quantize(Decimal("0.01"))

                if key not in tax_agg:
                    tax_agg[key] = {
                        "tax_type": tax.tax_type,
                        "selected_percentage": tax.selected_percentage,
                        "base_amount": Decimal("0.00"),
                        "calculated_amount": Decimal("0.00"),
                    }
                tax_agg[key]["base_amount"] += line_total
                tax_agg[key]["calculated_amount"] += calc

        # Create the aggregated AppliedTax records on the invoice
        ct = ContentType.objects.get_for_model(invoice)
        computed_total_tax = Decimal("0.00")

        for order, entry in enumerate(tax_agg.values()):
            computed_total_tax += entry["calculated_amount"]
            AppliedTax.objects.create(
                content_type=ct,
                object_id=invoice.id,
                tax_type=entry["tax_type"],
                selected_percentage=entry["selected_percentage"],
                base_amount=entry["base_amount"],
                calculated_amount=entry["calculated_amount"],
                order=order,
            )

        # ponytail: carry discount from source quotes, scaled by the invoicing ratio
        total_quote_subtotal = summary['total_invoiceable_subtotal']
        invoice_discount = Decimal("0.00")
        invoice_discount_breakdown = []

        if total_quote_subtotal > 0:
            ratio = subtotal / total_quote_subtotal  # how much of total we're invoicing
            for qd in summary['quotes']:
                quote = qd['quote']
                if quote.discount_amount and quote.discount_amount.amount > 0:
                    scaled = (quote.discount_amount.amount * ratio).quantize(Decimal("0.01"))
                    invoice_discount += scaled
                    for entry in (quote.discount_breakdown or []):
                        invoice_discount_breakdown.append({
                            **entry,
                            "amount": str((Decimal(entry["amount"]) * ratio).quantize(Decimal("0.01"))),
                            "base": str((Decimal(entry["base"]) * ratio).quantize(Decimal("0.01"))),
                        })

        invoice.subtotal_amount = subtotal
        invoice.discount_amount = invoice_discount
        invoice.discount_breakdown = invoice_discount_breakdown
        invoice.tax_amount = computed_total_tax
        invoice.total_amount = subtotal - invoice_discount + computed_total_tax
        invoice.save(update_fields=[
            'subtotal_amount', 'discount_amount', 'discount_amount_currency',
            'discount_breakdown', 'tax_amount', 'total_amount',
        ])

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


