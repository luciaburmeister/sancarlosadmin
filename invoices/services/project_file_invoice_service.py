import logging
from dataclasses import dataclass
from decimal import Decimal
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext_lazy as _

from invoices.models import (
    AppliedTax,
    InvoicePaymentStatus,
    SalesInvoice,
    SalesInvoiceDocumentType,
    SalesInvoiceItem,
    SalesInvoiceStatus,
)
from invoices.services.advances import AdvanceDeductionService, AdvanceInvoiceService
from invoices.services.discounts import (
    DiscountResolution,
    QuoteDiscountResolver,
    SourceLine,
    source_kind,
)
from invoices.services.sales_invoice_payment import resolve_sales_invoice_payment_method
from management.models import ProjectFile
from sales.models import Quote, QuoteStatus

logger = logging.getLogger(__name__)


@dataclass
class PricedLines:
    """What a set of invoice lines comes to, computed without writing anything.

    - ``subtotal``: sum of the line totals.
    - ``resolution``: the source quotes' discounts carried onto the lines
      (header ``discount_amount``/``breakdown`` + per-line taxable fractions).
    - ``tax_agg``: invoice-level taxes keyed by ``(tax_type_id, behavior, %)``,
      each ``{tax_type, selected_percentage, base_amount, calculated_amount}``
      on the lines' discounted bases.
    - ``groups``: taxable base per VAT group. A group's signature is the
      frozenset of ``(tax_type, %)`` a line carries (the same taxes that feed
      ``tax_agg``); lines without taxes share the empty frozenset.
    """

    subtotal: Decimal
    resolution: DiscountResolution
    tax_agg: dict
    groups: dict

    @property
    def tax_amount(self):
        """Additive taxes minus retentions (IRPF), as in SalesInvoice.recalculate_totals."""
        total = Decimal("0.00")
        for entry in self.tax_agg.values():
            if entry["tax_type"].behavior == "add":
                total += entry["calculated_amount"]
            else:
                total -= entry["calculated_amount"]
        return total

    @property
    def total_amount(self):
        return self.subtotal - self.resolution.discount_amount + self.tax_amount


class ProjectFileInvoiceService:
    """
    Service for creating Sales Invoices from ProjectFiles (Expedientes).
    Supports partial invoicing with validation to prevent over-invoicing.
    """

    @classmethod
    def get_accepted_quote_observations(cls, project_file):
        """
        Returns the non-empty, stripped observations of the ProjectFile's
        accepted quotes, in quote id order.
        """
        return [
            quote.observations.strip()
            for quote in Quote.objects.filter(
                project_file=project_file,
                status=QuoteStatus.ACCEPTED,
            ).order_by("id")
            if quote.observations and quote.observations.strip()
        ]

    @classmethod
    def get_invoiceable_summary(cls, project_file, with_groups=False, exclude_invoice=None):
        """
        Returns a dict with the invoicing status for a ProjectFile:
        - total_invoiceable: sum of accepted quotes' total_price + direct configurations' total_price
        - total_invoiced: sum of existing SalesInvoices linked to this ProjectFile
        - remaining: total_invoiceable - total_invoiced
        - quotes: list of accepted quotes
        - direct_configurations: list of direct (non-quote) configurations
        - invoices: list of existing invoices

        Cancelled invoices bill nothing and are left out of every figure.
        Money figures (total_invoiced, remaining, *_net) count advance
        invoices like any invoice; they are for display. The gross ratio
        base (total_invoiced_subtotal, remaining_subtotal) and the gates
        count goods only:
        - goods_remaining_net: net (taxable) goods base still to invoice —
          advance invoices left out, their deductions added back;
        - can_invoice: goods_remaining_net > 0.01;
        - advances: one entry per advance invoice (oldest first) with its
          base, deducted and pending amounts, overall and per line;
        - advances_open_net: base of the advances still to deduct;
        - advances_deductible_net: the part of it on FINAL advances with no
          cancellation pending or done at the AEAT.
        With ``with_groups``, also goods_remaining_net_by_group: the same
        figure per VAT group (keyed like PricedLines.groups), and
        invoiceable_net_by_group: the expediente's net base per VAT group.
        It re-prices the expediente and its invoices' lines, so it is opt-in.
        With ``exclude_invoice``, the expediente as if that invoice (its
        goods and its deductions) did not exist: what it can bill.
        """
        accepted_quotes = Quote.objects.filter(
            project_file=project_file,
            status=QuoteStatus.ACCEPTED,
        ).order_by('id')

        total_invoiceable = Decimal("0.00")
        total_invoiceable_subtotal = Decimal("0.00")
        # Net variants (after quote/invoice discounts) — used for display so
        # pre-discount quote subtotals aren't compared against discounted
        # invoice subtotals. The gross fields remain the ratio base for
        # partial invoicing math.
        total_invoiceable_subtotal_net = Decimal("0.00")
        quotes_data = []

        for quote in accepted_quotes:
            total_amt = Decimal(str(quote.total_price.amount))
            subtotal_amt = Decimal(str(quote.subtotal_price.amount))
            tax_amt = Decimal(str(quote.tax_amount.amount))
            discounted_sub = (
                Decimal(str(quote.discounted_subtotal.amount))
                if quote.discounted_subtotal else subtotal_amt
            )
            total_invoiceable += total_amt
            total_invoiceable_subtotal += subtotal_amt
            total_invoiceable_subtotal_net += discounted_sub
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
        ).exclude(product__exclude_from_invoicing=True)

        direct_configs_data = []
        for config in direct_configs:
            sub = Decimal(str(config.subtotal_price.amount))
            tot = Decimal(str(config.total_price.amount))
            total_invoiceable += tot
            total_invoiceable_subtotal += sub
            total_invoiceable_subtotal_net += sub
            direct_configs_data.append({
                'id': config.id,
                'name': config.name,
                'subtotal': sub,
                'total': tot,
                'units': config.units,
                'status': config.get_status_display(),
            })

        # Work entries (billable work linked to this project file). Their
        # total_price is pre-tax; taxes come from the entry's own AppliedTax
        # rows, so total_invoiceable gets the taxed figure like configs do.
        from management.models import WorkEntry

        work_entries = WorkEntry.objects.filter(
            project_file=project_file,
        ).select_related('entry_type').prefetch_related('applied_taxes__tax_type')

        work_entries_data = []
        for entry in work_entries:
            entry_total = Decimal(str(entry.total_price.amount))
            entry_with_tax = Decimal(str(entry.total_with_tax.amount))
            total_invoiceable += entry_with_tax
            total_invoiceable_subtotal += entry_total
            total_invoiceable_subtotal_net += entry_total
            work_entries_data.append({
                'id': entry.id,
                'description': entry.description,
                'quantity': entry.quantity,
                'unit_price': Decimal(str(entry.unit_price.amount)),
                'total': entry_total,
                'total_with_tax': entry_with_tax,
                'tax_amount': entry_with_tax - entry_total,
                'entry_type': entry.entry_type.name,
                'date': entry.date,
                'is_locked': entry.is_locked,
            })

        # Sum existing invoices linked to this project file
        # Source 1: single-PF invoices (FK on invoice)
        existing_invoices = (
            SalesInvoice.objects.filter(project_file=project_file)
            .exclude(status=SalesInvoiceStatus.CANCELLED)
            # What its advance deduction lines took off the subtotal (≤ 0)
            .annotate(deducted=Sum(
                'items__line_total', filter=Q(items__advance_line__isnull=False),
            ))
            .order_by('-date_issued')
        )
        if exclude_invoice is not None:
            existing_invoices = existing_invoices.exclude(pk=exclude_invoice.pk)

        total_invoiced = Decimal("0.00")
        total_invoiced_subtotal = Decimal("0.00")
        total_invoiced_subtotal_net = Decimal("0.00")
        goods_invoiced_net = Decimal("0.00")
        goods_net_by_invoice = {}  # goods invoice id → its goods net for this PF
        advance_invoices = []
        invoices_data = []

        for invoice in existing_invoices:
            inv_total = Decimal(str(invoice.total_amount.amount))
            inv_subtotal = Decimal(str(invoice.subtotal_amount.amount))
            inv_discount = (
                Decimal(str(invoice.discount_amount.amount))
                if invoice.discount_amount else Decimal("0.00")
            )
            total_invoiced += inv_total
            total_invoiced_subtotal_net += inv_subtotal - inv_discount
            if invoice.document_type == SalesInvoiceDocumentType.ADVANCE:
                advance_invoices.append(invoice)
            else:
                deducted = invoice.deducted or Decimal("0.00")
                total_invoiced_subtotal += inv_subtotal - deducted
                goods_net_by_invoice[invoice.id] = inv_subtotal - inv_discount - deducted
                goods_invoiced_net += goods_net_by_invoice[invoice.id]
            invoices_data.append({
                'id': invoice.id,
                'invoice_number': invoice.display_number,
                'status': invoice.get_status_display(),
                'document_type': invoice.document_type,
                'subtotal': inv_subtotal,
                'total': inv_total,
                'date_issued': invoice.date_issued,
            })

        # Source 2: bulk invoices (project_file FK on individual items)
        bulk_items = SalesInvoiceItem.objects.filter(
            project_file=project_file,
            invoice__project_file__isnull=True,  # only bulk invoices
        ).exclude(
            invoice__status=SalesInvoiceStatus.CANCELLED,
        ).select_related("invoice")
        if exclude_invoice is not None:
            bulk_items = bulk_items.exclude(invoice_id=exclude_invoice.pk)

        bulk_goods_items = []
        seen_bulk_invoices = set()
        for item in bulk_items:
            line_amount = Decimal(str(item.line_total.amount))
            total_invoiced_subtotal_net += line_amount
            # Advance deduction lines are not goods
            if item.advance_line_id is None:
                total_invoiced_subtotal += line_amount
                goods_invoiced_net += line_amount
                bulk_goods_items.append(item)
            # Count total for each bulk invoice only once
            if item.invoice_id not in seen_bulk_invoices:
                seen_bulk_invoices.add(item.invoice_id)
                # For bulk invoices, add the full invoice to the list
                inv = item.invoice
                invoices_data.append({
                    'id': inv.id,
                    'invoice_number': inv.invoice_number or f"{inv.display_number} (bulk)",
                    'status': inv.get_status_display(),
                    'document_type': inv.document_type,
                    'subtotal': Decimal("0"),  # per-item, not per-invoice
                    'total': Decimal("0"),
                    'date_issued': inv.date_issued,
                    'is_bulk': True,
                })

        remaining = total_invoiceable - total_invoiced
        remaining_subtotal = total_invoiceable_subtotal - total_invoiced_subtotal
        remaining_subtotal_net = total_invoiceable_subtotal_net - total_invoiced_subtotal_net
        goods_remaining_net = total_invoiceable_subtotal_net - goods_invoiced_net

        advances = cls._advances_data(
            advance_invoices, exclude_invoice_id=exclude_invoice.pk if exclude_invoice else None,
        )

        summary = {
            'total_invoiceable': total_invoiceable,
            'total_invoiceable_subtotal': total_invoiceable_subtotal,
            'total_invoiceable_subtotal_net': total_invoiceable_subtotal_net,
            'total_invoiced': total_invoiced,
            'total_invoiced_subtotal': total_invoiced_subtotal,
            'total_invoiced_subtotal_net': total_invoiced_subtotal_net,
            'remaining': remaining,
            'remaining_subtotal': remaining_subtotal,
            'remaining_subtotal_net': remaining_subtotal_net,
            'goods_remaining_net': goods_remaining_net,
            'can_invoice': goods_remaining_net > Decimal("0.01"),
            'advances': advances,
            'advances_open_net': sum(
                (a['pending'] for a in advances), Decimal("0.00"),
            ),
            'advances_deductible_net': sum(
                (a['pending'] for a in advances if a['deductible']), Decimal("0.00"),
            ),
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
        if with_groups:
            # The whole expediente priced at 100 %, squared with its net base
            summary['invoiceable_net_by_group'] = cls._reconcile_groups(
                cls.price_lines(
                    summary, cls.build_items_for_ratio(summary, Decimal("1"), split_configs=False),
                ).groups,
                total_invoiceable_subtotal_net,
            )
            summary['goods_remaining_net_by_group'] = cls._goods_remaining_net_by_group(
                summary, goods_net_by_invoice, bulk_goods_items,
            )
        return summary

    @classmethod
    def _advances_data(cls, advance_invoices, exclude_invoice_id=None):
        """The ``advances`` entries of get_invoiceable_summary, oldest first.

        An advance line's deducted amount counts its deduction lines on
        non-cancelled invoices, pro formas included (they reserve it), bar
        those of ``exclude_invoice_id``.
        """
        if not advance_invoices:
            return []
        from apps.einvoicing.models import (
            EInvoicingSubmission, SubmissionAction, SubmissionStatus,
        )

        counted = Q(deductions__invoice__status__in=[
            SalesInvoiceStatus.PROFORMA, SalesInvoiceStatus.FINAL,
        ])
        if exclude_invoice_id is not None:
            counted &= ~Q(deductions__invoice_id=exclude_invoice_id)
        advance_invoices = sorted(advance_invoices, key=lambda inv: inv.pk)
        lines_by_invoice = {}
        for line in (
            SalesInvoiceItem.objects.filter(invoice__in=advance_invoices)
            .annotate(deducted=Sum('deductions__line_total', filter=counted))
            .order_by('pk')
        ):
            base = Decimal(str(line.line_total.amount))
            deducted = -(line.deducted or Decimal("0.00"))
            lines_by_invoice.setdefault(line.invoice_id, []).append({
                'id': line.id,
                'base': base,
                'deducted': deducted,
                'pending': base - deducted,
            })
        # A cancellation queued, in flight or accepted at the AEAT takes the
        # advance out of reach; a rejected or failed one does not. Until the
        # AEAT answers (``cancelling``) the goods wait: it may come back.
        markers = list(
            EInvoicingSubmission.objects.filter(
                sales_invoice__in=advance_invoices,
                action=SubmissionAction.CANCEL,
                status__in=[
                    SubmissionStatus.PENDING,
                    SubmissionStatus.SUBMITTED,
                    SubmissionStatus.ACCEPTED,
                ],
            ).values_list('sales_invoice_id', 'status')
        )
        out_of_reach = {invoice_id for invoice_id, _status in markers}
        cancelling = {
            invoice_id for invoice_id, status in markers if status != SubmissionStatus.ACCEPTED
        }

        advances = []
        for invoice in advance_invoices:
            lines = lines_by_invoice.get(invoice.id, [])
            base = sum((line['base'] for line in lines), Decimal("0.00"))
            deducted = sum((line['deducted'] for line in lines), Decimal("0.00"))
            advances.append({
                'id': invoice.id,
                'invoice_number': invoice.display_number,
                'date_issued': invoice.date_issued,
                'status': invoice.status,
                'percentage': invoice.advance_percentage,
                'base': base,
                'deducted': deducted,
                'pending': base - deducted,
                'paid': invoice.payment_status == InvoicePaymentStatus.PAID,
                'deductible': (
                    invoice.status == SalesInvoiceStatus.FINAL
                    and invoice.id not in out_of_reach
                ),
                'cancelling': (
                    invoice.status == SalesInvoiceStatus.FINAL
                    and invoice.id in cancelling
                ),
                'lines': lines,
            })
        return advances

    @classmethod
    def _goods_remaining_net_by_group(cls, summary, goods_net_by_invoice, bulk_goods_items):
        """goods_remaining_net per VAT group: invoiceable_net_by_group (the
        whole expediente priced at 100 %) less each goods invoice's lines,
        re-priced the same way. Each pricing is squared with the figure the
        totals use, so the groups add up to goods_remaining_net."""
        remaining = dict(summary['invoiceable_net_by_group'])

        invoiced = []  # (lines, goods net they come to) per invoice
        lines_by_invoice = {}
        for line in SalesInvoiceItem.objects.filter(
            invoice_id__in=list(goods_net_by_invoice), advance_line__isnull=True,
        ).order_by('pk'):
            lines_by_invoice.setdefault(line.invoice_id, []).append(line)
        for invoice_id, goods_net in goods_net_by_invoice.items():
            invoiced.append((lines_by_invoice.get(invoice_id, []), goods_net))
        bulk_by_invoice = {}
        for line in bulk_goods_items:
            bulk_by_invoice.setdefault(line.invoice_id, []).append(line)
        for lines in bulk_by_invoice.values():
            invoiced.append((
                lines,
                sum((Decimal(str(line.line_total.amount)) for line in lines), Decimal("0.00")),
            ))

        for lines, goods_net in invoiced:
            for signature, amount in cls.goods_net_by_group(summary, lines, goods_net).items():
                remaining[signature] = remaining.get(signature, Decimal("0.00")) - amount
        return remaining

    @classmethod
    def goods_net_by_group(cls, summary, lines, goods_net):
        """``goods_net``, what an invoice's goods ``lines`` (SalesInvoiceItem)
        come to, per VAT group: the lines re-priced against the expediente's
        quotes, squared with that figure. Lines of no known kind (manual,
        legacy discount lines) are left to the reconciliation."""
        return cls._reconcile_groups(
            cls.price_lines(summary, cls.repriceable_items(lines)).groups, goods_net,
        )

    @staticmethod
    def repriceable_items(lines):
        """Invoice lines (SalesInvoiceItem) as price_lines() items, at the
        total each was stored with; lines of no known kind are left out."""
        return [
            {
                'source_id': line.source_id,
                'unit_price': Decimal(str(line.line_total.amount)),
                'quantity': Decimal("1"),
                'discount_percentage': Decimal("0.00"),
            }
            for line in lines
            if source_kind(line.source_id)
        ]

    @staticmethod
    def _reconcile_groups(groups, total):
        """``groups`` made to add up to ``total``: the cents per-line
        rounding leaves over (and lines no group claims) go to the largest
        group."""
        groups = dict(groups)
        difference = total - sum(groups.values(), Decimal("0.00"))
        if difference:
            largest = max(groups, key=lambda signature: abs(groups[signature]), default=frozenset())
            groups[largest] = groups.get(largest, Decimal("0.00")) + difference
        return groups

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
    def get_config_component_lines(cls, config) -> list:
        """A configuration's invoice lines: its pricing lines, else its BOM
        scaled to the subtotal, else one line for the whole subtotal."""
        override_subtotal = Decimal(str(config.subtotal_price.amount))

        pb = config.pricing_breakdown or {}
        lines = pb.get("lines", [])
        is_override_only = (
            len(lines) == 1 and lines[0].get("type") == "override"
        ) or (not lines and config.price_override is not None and config.price_override.amount >= 0)

        if not is_override_only and lines:
            return [
                {"label": line.get("label", ""), "amount": Decimal(str(line.get("amount", 0)))}
                for line in lines
                if Decimal(str(line.get("amount", 0))) != 0
            ]

        bom_lines = []
        bom_total = Decimal("0")
        for item in config.items.all():
            lt = Decimal(str(item.line_total.amount)) if item.line_total else Decimal("0")
            if lt <= 0:
                continue
            label = item.label or (item.component.name if item.component else "")
            if " × " in label:
                label = label.rsplit(" × ", 1)[0].strip()
            bom_lines.append({"label": label, "amount": lt})
            bom_total += lt

        if bom_lines:
            scale = override_subtotal / bom_total if bom_total else Decimal("1")
            return [
                {"label": bl["label"], "amount": (bl["amount"] * scale).quantize(Decimal("0.01"))}
                for bl in bom_lines
            ]

        return [{"label": config.name, "amount": override_subtotal}]

    @classmethod
    def build_items_for_ratio(cls, summary, ratio, show_vehicle_name=True, split_configs=True):
        """
        Invoice line dicts (for create_invoice_from_project_file) billing
        ``ratio`` of every source in ``summary``: configurations (split into
        their component lines), quote material lines, direct configurations
        and work entries scale; inventory items go in full. Negative sources
        are dropped, zero-value ones kept as 0 lines; ratio ≤ 0 gives no lines.

        With ``split_configs=False`` each quote configuration is one line at
        its subtotal instead: its component lines need not add up to it
        (the BOM base and hidden discount rules are no line of their own).
        """
        items = []

        if ratio <= 0:
            return items

        for quote_data in summary["quotes"]:
            quote = quote_data["quote"]
            quote_name = quote.project_name if show_vehicle_name else "."
            for config in quote.configurations.prefetch_related("items__component").all():
                sub = Decimal(str(config.subtotal_price.amount))
                # Zero-value configs are kept as a single 0-amount line
                if sub < 0:
                    continue
                if not split_configs:
                    items.append({
                        "source_id": f"config_{config.id}",
                        "description": f"{config.name} (Q#{quote.id})",
                        "quote_name": quote_name,
                        "quantity": Decimal("1"),
                        "unit_price": (sub * ratio).quantize(Decimal("0.01")),
                        "discount_percentage": Decimal("0.00"),
                    })
                    continue
                for comp in cls.get_config_component_lines(config):
                    comp_price = (comp["amount"] * ratio).quantize(Decimal("0.01"))
                    if comp_price < 0:
                        continue
                    items.append({
                        "source_id": f"config_{config.id}",
                        "description": comp["label"],
                        "quote_name": quote_name,
                        "quantity": Decimal("1"),
                        "unit_price": comp_price,
                        "discount_percentage": Decimal("0.00"),
                    })

            for item in quote.items.all():
                item_price = (item.line_total.amount * ratio).quantize(Decimal("0.01"))
                if item_price < 0:
                    continue
                items.append({
                    "source_id": f"quote_item_{item.id}",
                    "description": f"{item.material} x{item.quantity} (P#{quote.id})",
                    "quote_name": quote_name,
                    "quantity": Decimal("1"),
                    "unit_price": item_price,
                    "discount_percentage": Decimal("0.00"),
                })

            for inv_item in quote.inventory_items.select_related("inventory_item").all():
                line_amt = inv_item.line_total.amount if inv_item.line_total else Decimal("0.00")
                if line_amt < 0:
                    continue
                inv_name = (
                    inv_item.inventory_item.name
                    if inv_item.inventory_item_id
                    else f"Inventory Item #{inv_item.pk}"
                )
                items.append({
                    "source_id": f"inv_item_{inv_item.id}",
                    "description": inv_name,
                    "quote_name": quote_name,
                    "quantity": inv_item.quantity,
                    "unit_price": inv_item.unit_price.amount,
                    "discount_percentage": inv_item.discount,
                })

        for config_data in summary.get("direct_configurations", []):
            sub = config_data["subtotal"]
            item_price = (sub * ratio).quantize(Decimal("0.01"))
            if item_price < 0:
                continue
            items.append({
                "source_id": f"direct_{config_data['id']}",
                "description": config_data["name"],
                "quantity": Decimal("1"),
                "unit_price": item_price,
                "discount_percentage": Decimal("0.00"),
            })

        for entry_data in summary.get("work_entries", []):
            item_price = (entry_data["total"] * ratio).quantize(Decimal("0.01"))
            if item_price < 0:
                continue
            items.append({
                "source_id": f"work_entry_{entry_data['id']}",
                "description": entry_data["description"],
                "quantity": Decimal("1"),
                "unit_price": item_price,
                "discount_percentage": Decimal("0.00"),
            })

        return items

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
            ).exclude(product__exclude_from_invoicing=True).values_list('id', flat=True)
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

    @staticmethod
    def line_total(item_data):
        """A line's total as SalesInvoiceItem.save computes it: unit price ×
        quantity less its line discount, before the database rounds it."""
        unit_price = item_data['unit_price']
        unit_price = getattr(unit_price, 'amount', unit_price)
        gross = unit_price * item_data.get('quantity', Decimal("1"))
        discount = gross * (item_data.get('discount_percentage', Decimal("0.00")) / Decimal("100"))
        return gross - discount

    @classmethod
    def price_lines(cls, summary, items_data):
        """
        Prices invoice lines without writing anything (see PricedLines).

        Taxes are computed per line item from each source's own AppliedTax
        records (Configuration, QuoteInventoryItem or WorkEntry), on the
        line's base after its quote's discount, supporting mixed rates. A
        configuration or inventory line of a quote taxed in client-tax mode
        takes the quote's header taxes instead, as the quote and its
        delivery orders tax it (DeliveryOrderService._source_taxes_and_quote).

        Args:
            summary: get_invoiceable_summary() of the ProjectFile; its
                     accepted quotes own the discounts carried onto the lines
            items_data: List of dicts with description, quantity, unit_price,
                        discount_percentage, and source_id
        """
        from configurator.models import Configuration
        from invoices.services.delivery_order_service import DeliveryOrderService
        from management.models import WorkEntry
        from sales.models import QuoteInventoryItem, QuoteItem

        config_ct = ContentType.objects.get_for_model(Configuration)
        inv_item_ct = ContentType.objects.get_for_model(QuoteInventoryItem)
        work_entry_ct = ContentType.objects.get_for_model(WorkEntry)

        subtotal = Decimal("0.00")
        line_taxes = []  # (line_total, source_taxes, quote_id, kind) — taxed after the discount is known
        source_lines = []  # SourceLine records for the discount resolver
        header_taxes = {}  # quote id → its client-tax header taxes, or None

        def own_taxes(source_ct, obj_id, quote_id):
            # A client-tax quote taxes the line with its header taxes
            if quote_id is not None:
                taxes = DeliveryOrderService._quote_header_taxes(quote_id, header_taxes)
                if taxes is not None:
                    return taxes
            return list(
                AppliedTax.objects.filter(
                    content_type=source_ct, object_id=obj_id
                ).select_related("tax_type")
            )

        for item_data in items_data:
            line_total = cls.line_total(item_data)
            subtotal += line_total

            # Resolve the source's taxes and owning quote from its source_id
            source_id = item_data.get('source_id', '')
            source_taxes = []
            quote_id = None

            if source_id.startswith("config_") or source_id.startswith("direct_"):
                obj_id = source_id.split("_", 1)[1]
                quote_id = (
                    Configuration.objects.filter(pk=obj_id)
                    .values_list("quote_id", flat=True).first()
                )
                source_taxes = own_taxes(config_ct, obj_id, quote_id)
            elif source_id.startswith("inv_item_"):
                obj_id = source_id.split("_", 2)[2]
                quote_id = (
                    QuoteInventoryItem.objects.filter(pk=obj_id)
                    .values_list("quote_id", flat=True).first()
                )
                source_taxes = own_taxes(inv_item_ct, obj_id, quote_id)
            elif source_id.startswith("quote_item_"):
                # quote_item_ lines have no taxes
                obj_id = source_id.split("_", 2)[2]
                quote_id = (
                    QuoteItem.objects.filter(pk=obj_id)
                    .values_list("quote_id", flat=True).first()
                )
            elif source_id.startswith("work_entry_"):
                # Work entries carry their own AppliedTax and belong to no quote
                obj_id = source_id.split("_", 2)[2]
                source_taxes = list(
                    AppliedTax.objects.filter(
                        content_type=work_entry_ct, object_id=obj_id
                    ).select_related("tax_type")
                )

            kind = source_kind(source_id)
            source_lines.append(SourceLine(source_id, line_total, quote_id, kind))
            line_taxes.append((line_total, source_taxes, quote_id, kind))

        # Carry discounts from source quotes via the per-level strategies,
        # each scaled by how much of THAT quote's base this invoice covers —
        # a quote's discount must not bleed into lines invoiced from other
        # quotes.
        resolution = QuoteDiscountResolver().resolve(
            [qd['quote'] for qd in summary['quotes']], source_lines,
        )

        # Taxes apply to the discounted base (the quote's "taxable base"),
        # so reduce each line by its own quote's discount fraction for its
        # line kind.
        # Aggregation: (tax_type_id, behavior, percentage) → {tax_type, base, calc}
        tax_agg: dict[tuple, dict] = {}
        groups: dict[frozenset, Decimal] = {}
        for line_total, source_taxes, quote_id, kind in line_taxes:
            frac = resolution.taxable_fraction(quote_id, kind)
            taxable = (line_total * (Decimal("1") - frac)).quantize(Decimal("0.01"))
            signature = set()
            for tax in source_taxes:
                if tax.selected_percentage <= 0:
                    continue
                signature.add((tax.tax_type, tax.selected_percentage))
                key = (tax.tax_type_id, tax.tax_type.behavior, tax.selected_percentage)
                rate = tax.selected_percentage / Decimal("100")
                calc = (taxable * rate).quantize(Decimal("0.01"))

                if key not in tax_agg:
                    tax_agg[key] = {
                        "tax_type": tax.tax_type,
                        "selected_percentage": tax.selected_percentage,
                        "base_amount": Decimal("0.00"),
                        "calculated_amount": Decimal("0.00"),
                    }
                tax_agg[key]["base_amount"] += taxable
                tax_agg[key]["calculated_amount"] += calc
            signature = frozenset(signature)
            groups[signature] = groups.get(signature, Decimal("0.00")) + taxable

        return PricedLines(subtotal, resolution, tax_agg, groups)

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

        Lines are priced by price_lines(): taxes per line item from each
        source's own AppliedTax records, supporting mixed rates.

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
            ValidationError if the invoice's goods would exceed the remaining
            invoiceable (net) amount, or an advance of the expediente is
            still a pro forma or being cancelled at the AEAT
        """
        cls.lock_project_files([project_file.pk])
        AdvanceInvoiceService.lock_advances([project_file.pk])
        AdvanceInvoiceService.check_no_proforma_advance([project_file.pk])
        summary = cls.get_invoiceable_summary(project_file)

        if summary['total_invoiceable'] <= 0:
            raise ValidationError(
                _("This project file has no accepted quotes, direct configurations or work entries. Cannot create an invoice.")
            )

        resolved_payment_method = resolve_sales_invoice_payment_method(
            client=client,
            explicit=payment_method,
        )

        priced = cls.price_lines(summary, items_data)
        goods_net = priced.subtotal - priced.resolution.discount_amount
        AdvanceDeductionService.check(summary, goods_net)

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

        invoiced_source_ids = []
        for item_data in items_data:
            SalesInvoiceItem.objects.create(
                invoice=invoice,
                description=item_data['description'],
                quote_name=item_data.get('quote_name', ''),
                quantity=item_data.get('quantity', Decimal("1")),
                unit_price=item_data['unit_price'],
                discount_percentage=item_data.get('discount_percentage', Decimal("0.00")),
                source_id=item_data.get('source_id', ''),
            )
            invoiced_source_ids.append(item_data.get('source_id', ''))

        # Create the aggregated AppliedTax records on the invoice
        ct = ContentType.objects.get_for_model(invoice)
        for order, entry in enumerate(priced.tax_agg.values()):
            AppliedTax.objects.create(
                content_type=ct,
                object_id=invoice.id,
                tax_type=entry["tax_type"],
                selected_percentage=entry["selected_percentage"],
                base_amount=entry["base_amount"],
                calculated_amount=entry["calculated_amount"],
                order=order,
            )

        invoice.discount_amount = priced.resolution.discount_amount
        invoice.discount_breakdown = priced.resolution.breakdown
        # Each rate's tax on its base, as SalesInvoice.recalculate_totals
        # rounds it (so saving the pro forma again moves no cent);
        # retentions (IRPF) subtract
        invoice.recalculate_totals(save=False)
        invoice.save(update_fields=[
            'subtotal_amount', 'discount_amount', 'discount_amount_currency',
            'discount_breakdown', 'tax_amount', 'total_amount',
        ])

        cls.lock_invoiced_work_entries(invoiced_source_ids)

        logger.info(
            "Created SalesInvoice #%s from ProjectFile %s. "
            "Amount: %s (subtotal: %s, tax: %s). Goods remaining (net): %s",
            invoice.id, project_file.reference,
            invoice.total_amount, invoice.subtotal_amount,
            invoice.tax_amount, summary['goods_remaining_net'] - goods_net,
        )

        return invoice

    @classmethod
    def lock_project_files(cls, project_file_ids):
        """Row-lock expedientes, in pk order, for the rest of the transaction.

        Every path that writes an expediente's invoices takes these locks
        first — before advance invoice rows and the proforma/invoice
        numbering rows — so two of them can never deadlock: saving an
        invoice updates its expedientes' cached totals, i.e. locks them.
        """
        ids = sorted({pk for pk in project_file_ids if pk})
        if ids:
            list(
                ProjectFile.objects.select_for_update()
                .filter(pk__in=ids).order_by('pk').values_list('pk', flat=True)
            )

    @classmethod
    def project_file_ids_of(cls, invoice):
        """Every expediente an invoice counts toward: its own, the bulk
        invoice's selection (M2M) and the one tagged on each line."""
        ids = {invoice.project_file_id} if invoice.project_file_id else set()
        if invoice.pk:
            ids.update(invoice.project_files.values_list('pk', flat=True))
            ids.update(
                invoice.items.filter(project_file__isnull=False)
                .values_list('project_file_id', flat=True)
            )
        return ids

    WORK_ENTRY_SOURCE_PREFIX = "work_entry_"

    @classmethod
    def work_entry_ids_from_source_ids(cls, source_ids):
        """Extract WorkEntry pks from ``work_entry_<id>`` source ids."""
        prefix = cls.WORK_ENTRY_SOURCE_PREFIX
        ids = []
        for source_id in source_ids:
            if not source_id or not source_id.startswith(prefix):
                continue
            raw = source_id[len(prefix):]
            if raw.isdigit():
                ids.append(int(raw))
        return ids

    @classmethod
    def lock_invoiced_work_entries(cls, source_ids):
        """Lock every WorkEntry referenced by the given invoice line source ids
        so its pricing can no longer be edited (see WorkEntry.clean)."""
        from management.models import WorkEntry

        ids = cls.work_entry_ids_from_source_ids(source_ids)
        if ids:
            WorkEntry.objects.filter(pk__in=ids, is_locked=False).update(is_locked=True)

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
            ).exclude(product__exclude_from_invoicing=True).values_list('id', flat=True)
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


