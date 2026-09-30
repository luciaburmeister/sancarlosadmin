"""
Service for creating a single SalesInvoice from multiple ProjectFiles.

Invoices 100% of the remaining amount for each selected ProjectFile,
tagging each line item with its source PF for tracking, and deducts the
advances each of them has left to deduct.
"""
import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from invoices.models import (
    SalesInvoice, SalesInvoiceItem, SalesInvoiceStatus, AppliedTax,
)
from invoices.services.advances import AdvanceDeductionService, AdvanceInvoiceService
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from invoices.services.sales_invoice_payment import resolve_sales_invoice_payment_method

logger = logging.getLogger(__name__)


class BulkProjectFileInvoiceService:
    """Creates a single SalesInvoice combining items from multiple ProjectFiles."""

    @classmethod
    @transaction.atomic
    def create_bulk_invoice(
        cls, project_files, company, client,
        date_issued, due_date=None, notes=None, payment_method=None,
    ):
        """
        Create one SalesInvoice that invoices 100 % of the remaining amount
        for every ProjectFile in the list, less the advances each of them
        has left to deduct: the invoice closes every expediente it bills.

        Args:
            project_files: queryset or list of ProjectFile instances
            company: Company for the invoice
            client: Client for the invoice
            date_issued: Invoice date
            due_date: Optional due date
            notes: Optional notes
            payment_method: Optional override

        Returns:
            SalesInvoice instance

        Raises:
            ValidationError on validation failures
        """
        pf_list = list(project_files)

        # ---------- validation ----------
        if not pf_list:
            raise ValidationError(_("No project files selected."))

        client_ids = {pf.client_id for pf in pf_list}
        if len(client_ids) > 1:
            raise ValidationError(
                _("All selected project files must belong to the same client.")
            )

        company_ids = {pf.company_id for pf in pf_list}
        if len(company_ids) > 1:
            raise ValidationError(
                _("All selected project files must belong to the same company.")
            )

        # Expedientes first, then their advances, before any numbering lock
        pf_ids = [pf.pk for pf in pf_list]
        ProjectFileInvoiceService.lock_project_files(pf_ids)
        AdvanceInvoiceService.lock_advances(pf_ids)

        # ---------- price each PF's lines ----------
        billed = []  # (pf, items, PricedLines) of each PF with goods left

        for pf in pf_list:
            summary = ProjectFileInvoiceService.get_invoiceable_summary(pf)
            if not summary["can_invoice"]:
                continue  # goods already fully invoiced, skip

            # Build items at 100 % of remaining
            items = cls._build_items_for_pf(pf, summary)
            if not items:
                continue
            # Each PF's quotes carry their own discounts onto its lines
            priced = ProjectFileInvoiceService.price_lines(summary, items)
            # The same net over-invoicing check as a single expediente's invoice
            AdvanceDeductionService.check(
                summary, priced.subtotal - priced.resolution.discount_amount,
            )
            billed.append((pf, items, priced))

        if not billed:
            raise ValidationError(
                _("All selected project files are already fully invoiced.")
            )
        AdvanceInvoiceService.check_no_proforma_advance([pf.pk for pf, _items, _priced in billed])

        # ---------- create invoice ----------
        resolved_pm = resolve_sales_invoice_payment_method(
            client=client,
            explicit=payment_method,
        )

        invoice = SalesInvoice.objects.create(
            company=company,
            status=SalesInvoiceStatus.PROFORMA,
            invoice_number=None,
            date_issued=date_issued,
            due_date=due_date,
            client=client,
            project_file=None,  # multi-PF → no single FK
            notes=notes,
            payment_method=resolved_pm,
        )

        # ---------- create line items + aggregate taxes ----------
        discount = Decimal("0.00")
        breakdown = []
        tax_agg: dict[tuple, dict] = {}

        for pf, items, priced in billed:
            for item_data in items:
                SalesInvoiceItem.objects.create(
                    invoice=invoice,
                    description=item_data["description"],
                    quote_name=item_data.get("quote_name", ""),
                    quantity=item_data.get("quantity", Decimal("1")),
                    unit_price=item_data["unit_price"],
                    discount_percentage=item_data.get("discount_percentage", Decimal("0.00")),
                    source_id=item_data.get("source_id", ""),
                    project_file_id=pf.pk,
                )
            discount += priced.resolution.discount_amount
            breakdown.extend(priced.resolution.breakdown)
            for key, entry in priced.tax_agg.items():
                if key not in tax_agg:
                    tax_agg[key] = {
                        "tax_type": entry["tax_type"],
                        "selected_percentage": entry["selected_percentage"],
                        "base_amount": Decimal("0.00"),
                        "calculated_amount": Decimal("0.00"),
                    }
                tax_agg[key]["base_amount"] += entry["base_amount"]
                tax_agg[key]["calculated_amount"] += entry["calculated_amount"]

        # Create aggregated AppliedTax records
        ct = ContentType.objects.get_for_model(invoice)

        for order, entry in enumerate(tax_agg.values()):
            AppliedTax.objects.create(
                content_type=ct,
                object_id=invoice.id,
                tax_type=entry["tax_type"],
                selected_percentage=entry["selected_percentage"],
                base_amount=entry["base_amount"],
                calculated_amount=entry["calculated_amount"],
                order=order,
            )

        # ---------- update invoice totals ----------
        invoice.discount_amount = discount
        invoice.discount_breakdown = breakdown
        # Each rate's tax on its base, as SalesInvoice.recalculate_totals
        # rounds it (so saving the pro forma again moves no cent);
        # retentions (IRPF) subtract
        invoice.recalculate_totals(save=False)
        invoice.save(update_fields=[
            "subtotal_amount", "discount_amount", "discount_amount_currency",
            "discount_breakdown", "tax_amount", "total_amount",
        ])

        ProjectFileInvoiceService.lock_invoiced_work_entries(
            [item_data.get("source_id", "") for _pf, items, _priced in billed for item_data in items]
        )

        # Link M2M
        invoice.project_files.set(pf_list)

        # It bills what each PF has left, so it takes all of its advances
        AdvanceDeductionService.apply(invoice)

        # Recalculate all affected PFs
        for pf in pf_list:
            pf.recalculate_total_amount()

        logger.info(
            "Created bulk SalesInvoice #%s for %d ProjectFiles. "
            "Amount: %s (subtotal: %s, tax: %s)",
            invoice.id, len(pf_list),
            invoice.total_amount, invoice.subtotal_amount, invoice.tax_amount,
        )

        return invoice

    @classmethod
    def _build_items_for_pf(cls, project_file, summary):
        """
        Build item dicts for 100% of remaining on a single ProjectFile, as
        the expediente's own invoices build them (build_items_for_ratio),
        each configuration one line at its subtotal. Each description is
        prefixed with the PF reference.
        """
        pf_ref = project_file.reference or f"PF#{project_file.pk}"
        ratio = ProjectFileInvoiceService.compute_invoice_line_ratio(
            summary, "percentage", Decimal("100"),
        )
        items = ProjectFileInvoiceService.build_items_for_ratio(summary, ratio, split_configs=False)
        for item in items:
            item["description"] = f"[{pf_ref}] {item['description']}"
            item["project_file_id"] = project_file.pk
        return items
