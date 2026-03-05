import logging
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from invoices.models import SalesInvoice, SalesInvoiceItem, AppliedTax, SalesInvoiceStatus
from sales.models import DeliveryOrder, DeliveryOrderItem
from configurator.models import ExportTemplateKind, ConfigurationExportTemplate
from configurator.services.export.service import ConfigurationExportService

logger = logging.getLogger(__name__)

class SalesInvoiceService:

    @classmethod
    @transaction.atomic
    def create_from_configuration(cls, configuration):
        """
        Generates a SalesInvoice (Pro Forma) and a Delivery Order from a Configuration.
        """
        company = configuration.company
        client_id = getattr(configuration, 'client_id', None)
        
        sales_invoice = SalesInvoice.objects.create(
            company=company,
            status=SalesInvoiceStatus.PROFORMA,
            invoice_number=None, # Assigned on finalization
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

        # [NEW] Generate Delivery Order immediately
        cls._create_delivery_order(sales_invoice)
        
        return sales_invoice

    @classmethod
    @transaction.atomic
    def finalize_invoice(cls, sales_invoice):
        """
        Converts Pro Forma to Final, assigns invoice number.
        """
        if sales_invoice.status == SalesInvoiceStatus.FINAL:
            return sales_invoice

        company = sales_invoice.company
        sales_invoice.status = SalesInvoiceStatus.FINAL
        
        # Invoices logic
        next_invoice = cls._generate_next_invoice_data(company)
        sales_invoice.invoice_number = next_invoice['number']
        sales_invoice.incremental_id = next_invoice['incremental_id']
        sales_invoice.save(update_fields=['status', 'invoice_number', 'incremental_id'])

        return sales_invoice

    @classmethod
    def _create_delivery_order(cls, sales_invoice):
        """
        Creates a Delivery Order based on the configuration's product.
        """
        company = sales_invoice.company
        configuration = sales_invoice.configuration
        if not configuration:
            return None

        next_do = cls._generate_next_delivery_order_data(company)
        delivery_order = DeliveryOrder.objects.create(
            company=company,
            number=next_do['number'],
            incremental_id=next_do['incremental_id'],
            date=timezone.now().date(),
            client=sales_invoice.client,
            sales_invoice=sales_invoice,
            configuration=configuration
        )

        # Delivery Order items reflect the finished product, not BOM items
        DeliveryOrderItem.objects.create(
            delivery_order=delivery_order,
            description=str(configuration.product),
            quantity=Decimal("1.000") # Assuming 1 product per configuration for now
        )
        
        return delivery_order

    @classmethod
    def _generate_next_invoice_data(cls, company):
        """
        Generates the next sequential invoice number and incremental ID for a company.
        Format: INV-YYYY-SEQ (e.g., INV-2026-00001)
        """
        year = timezone.now().year
        prefix = f"INV-{year}-"
        
        # Lock for update - only consider finalized invoices
        last_invoice = SalesInvoice.objects.filter(
            company=company,
            incremental_id__isnull=False
        ).select_for_update().order_by('-incremental_id').first()
        
        next_id = (last_invoice.incremental_id + 1) if last_invoice else 1
        
        return {
            'number': f"{prefix}{next_id:05d}",
            'incremental_id': next_id
        }

    @classmethod
    def _generate_next_delivery_order_data(cls, company):
        """
        Generates the next sequential delivery order number and incremental ID for a company.
        Format: DO-YYYY-SEQ (e.g., DO-2026-00001)
        """
        year = timezone.now().year
        prefix = f"DO-{year}-"
        
        last_do = DeliveryOrder.objects.filter(
            company=company,
            incremental_id__isnull=False
        ).select_for_update().order_by('-incremental_id').first()
        
        next_id = (last_do.incremental_id + 1) if last_do else 1
            
        return {
            'number': f"{prefix}{next_id:05d}",
            'incremental_id': next_id
        }

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
            product=sales_invoice.configuration.product if sales_invoice.configuration else None,
            kind=ExportTemplateKind.SALES_INVOICE
        )
        
        if not template:
            logger.warning(f"No Sales Invoice template found for product {sales_invoice.configuration.product}")
            return None

        return ConfigurationExportService.render_html(sales_invoice, template)
