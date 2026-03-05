import logging
from django.db import transaction
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from invoices.models import SalesInvoice, SalesInvoiceItem, AppliedTax
from configurator.models import ExportTemplateKind, ConfigurationExportTemplate
from configurator.services.export.service import ConfigurationExportService

logger = logging.getLogger(__name__)

class SalesInvoiceService:

    @classmethod
    @transaction.atomic
    def create_from_configuration(cls, configuration):
        """
        Generates a SalesInvoice from a Configuration.
        """
        company = configuration.company
        
        invoice_number = cls._generate_next_invoice_number(company)
        client_id = getattr(configuration, 'client_id', None)
        
        sales_invoice = SalesInvoice.objects.create(
            company=company,
            invoice_number=invoice_number,
            date_issued=timezone.now().date(),
            client_id=client_id,
            configuration=configuration,
            notes=f"Generated from Configuration #{configuration.id}",
            subtotal_amount=configuration.subtotal_price,
            total_amount=configuration.total_price,
            tax_amount=configuration.total_price - configuration.subtotal_price
        )
        
        for item in configuration.items.filter(is_template_item=True):
             SalesInvoiceItem.objects.create(
                 invoice=sales_invoice,
                 description=str(item.component),
                 quantity=item.quantity,
                 unit_price=item.unit_price,
                 line_total=item.line_total
             )

        cls._copy_taxes(configuration, sales_invoice)
        
        # 4. Recalculate totals to ensure accurate tax amounts and totals
        sales_invoice.recalculate_totals()
        
        return sales_invoice

    @classmethod
    def _generate_next_invoice_number(cls, company):
        """
        Generates the next sequential invoice number for a company.
        Format: INV-YYYY-SEQ (e.g., INV-2026-00001)
        """
        year = timezone.now().year
        prefix = f"INV-{year}-"
        
        # Lock for update to avoid race conditions
        last_invoice = SalesInvoice.objects.filter(
            company=company,
            invoice_number__startswith=prefix
        ).select_for_update().order_by('-invoice_number').first()
        
        if last_invoice and last_invoice.invoice_number:
            try:
                # Assuming number is after the prefix
                parts = last_invoice.invoice_number.split('-')
                last_seq = int(parts[-1])
                new_seq = last_seq + 1
            except (ValueError, IndexError):
                new_seq = 1
        else:
            new_seq = 1
            
        return f"{prefix}{new_seq:05d}"

    @classmethod
    def _copy_taxes(cls, source, target):
        """
        Copies AppliedTax records from source to target.
        """
        source_ct = ContentType.objects.get_for_model(source)
        target_ct = ContentType.objects.get_for_model(target)
        
        taxes = AppliedTax.objects.filter(content_type=source_ct, object_id=source.id)
        for tax in taxes:
            AppliedTax.objects.create(
                content_type=target_ct,
                object_id=target.id,
                tax_type=tax.tax_type,
                selected_percentage=tax.selected_percentage,
                base_amount=tax.base_amount,
                calculated_amount=tax.calculated_amount,
                order=tax.order
            )

    @classmethod
    def render_pdf(cls, sales_invoice):
        """
        Renders the invoice PDF using a SALES_INVOICE template.
        """
        template = ConfigurationExportTemplate.get_default_for_product(
            product=sales_invoice.configuration.product,
            kind=ExportTemplateKind.SALES_INVOICE
        )
        
        if not template:
            logger.warning(f"No Sales Invoice template found for product {sales_invoice.configuration.product}")
            return None

        return ConfigurationExportService.render_html(sales_invoice, template)
