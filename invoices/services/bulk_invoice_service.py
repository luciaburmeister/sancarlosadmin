"""
Service for creating a single SalesInvoice from multiple ProjectFiles.

Invoices 100% of the remaining amount for each selected ProjectFile,
tagging each line item with its source PF for tracking.
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
        for every ProjectFile in the list.

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

        # ---------- gather items from each PF ----------
        all_items_data = []       # list of dicts ready for SalesInvoiceItem.objects.create
        pf_summaries = {}         # pf.pk → summary dict

        for pf in pf_list:
            summary = ProjectFileInvoiceService.get_invoiceable_summary(pf)
            pf_summaries[pf.pk] = summary

            if summary["remaining"] <= Decimal("0.01"):
                continue  # already fully invoiced, skip

            # Build items at 100 % of remaining
            items = cls._build_items_for_pf(pf, summary)
            all_items_data.extend(items)

        if not all_items_data:
            raise ValidationError(
                _("All selected project files are already fully invoiced.")
            )

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
        from configurator.models import Configuration
        from sales.models import QuoteInventoryItem

        config_ct = ContentType.objects.get_for_model(Configuration)
        inv_item_ct = ContentType.objects.get_for_model(QuoteInventoryItem)

        subtotal = Decimal("0.00")
        tax_agg: dict[tuple, dict] = {}

        for item_data in all_items_data:
            item = SalesInvoiceItem.objects.create(
                invoice=invoice,
                description=item_data["description"],
                quote_name=item_data.get("quote_name", ""),
                quantity=item_data.get("quantity", Decimal("1")),
                unit_price=item_data["unit_price"],
                discount_percentage=item_data.get("discount_percentage", Decimal("0.00")),
                source_id=item_data.get("source_id", ""),
                project_file_id=item_data["project_file_id"],
            )
            line_total = item.line_total.amount
            subtotal += line_total

            # Resolve source taxes
            source_id = item_data.get("source_id", "")
            source_taxes = []

            if source_id.startswith("config_") or source_id.startswith("direct_"):
                obj_id = source_id.split("_", 1)[1]
                source_taxes = list(
                    AppliedTax.objects.filter(
                        content_type=config_ct, object_id=obj_id,
                    ).select_related("tax_type")
                )
            elif source_id.startswith("inv_item_"):
                obj_id = source_id.split("_", 2)[2]
                source_taxes = list(
                    AppliedTax.objects.filter(
                        content_type=inv_item_ct, object_id=obj_id,
                    ).select_related("tax_type")
                )

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

        # Create aggregated AppliedTax records
        ct = ContentType.objects.get_for_model(invoice)
        total_tax = Decimal("0.00")

        for order, entry in enumerate(tax_agg.values()):
            total_tax += entry["calculated_amount"]
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
        invoice.subtotal_amount = subtotal
        invoice.tax_amount = total_tax
        invoice.total_amount = subtotal + total_tax
        invoice.save(update_fields=[
            "subtotal_amount", "tax_amount", "total_amount",
        ])

        # Link M2M
        invoice.project_files.set(pf_list)

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
        Build item dicts for 100% of remaining on a single ProjectFile.
        Each dict has project_file_id set for item-level tracking.
        """
        items = []
        pf_ref = project_file.reference or f"PF#{project_file.pk}"

        for quote_data in summary["quotes"]:
            quote = quote_data["quote"]

            for config in quote.configurations.all():
                sub = config.subtotal_price.amount
                if sub <= 0:
                    continue
                if config.units and config.units > 0:
                    qty = config.units
                    up = (sub / qty).quantize(Decimal("0.01"))
                else:
                    qty = Decimal("1")
                    up = sub
                items.append({
                    "source_id": f"config_{config.id}",
                    "description": f"[{pf_ref}] {config.name} (Q#{quote.id})",
                    "quote_name": quote.project_name or "",
                    "quantity": qty,
                    "unit_price": up,
                    "discount_percentage": Decimal("0.00"),
                    "project_file_id": project_file.pk,
                })

            for item in quote.items.all():
                line_amt = item.line_total.amount
                if line_amt <= 0:
                    continue
                items.append({
                    "source_id": f"quote_item_{item.id}",
                    "description": f"[{pf_ref}] {item.material} x{item.quantity} (Q#{quote.id})",
                    "quote_name": quote.project_name or "",
                    "quantity": Decimal("1"),
                    "unit_price": line_amt,
                    "discount_percentage": Decimal("0.00"),
                    "project_file_id": project_file.pk,
                })

            for inv_item in quote.inventory_items.select_related("inventory_item").all():
                line_amt = inv_item.line_total.amount if inv_item.line_total else Decimal("0.00")
                if line_amt <= 0:
                    continue
                desc = inv_item.inventory_item.name if inv_item.inventory_item else inv_item.description
                items.append({
                    "source_id": f"inv_item_{inv_item.id}",
                    "description": f"[{pf_ref}] {desc}",
                    "quote_name": quote.project_name or "",
                    "quantity": inv_item.quantity,
                    "unit_price": inv_item.unit_price.amount,
                    "discount_percentage": Decimal("0.00"),
                    "project_file_id": project_file.pk,
                })

        # Direct configurations (not linked to any quote)
        for config_data in summary["direct_configurations"]:
            from configurator.models import Configuration
            config = Configuration.objects.get(pk=config_data["id"])
            sub = config.subtotal_price.amount
            if sub <= 0:
                continue
            if config.units and config.units > 0:
                qty = config.units
                up = (sub / qty).quantize(Decimal("0.01"))
            else:
                qty = Decimal("1")
                up = sub
            items.append({
                "source_id": f"direct_{config.id}",
                "description": f"[{pf_ref}] {config.name}",
                "quote_name": "",
                "quantity": qty,
                "unit_price": up,
                "discount_percentage": Decimal("0.00"),
                "project_file_id": project_file.pk,
            })

        return items
