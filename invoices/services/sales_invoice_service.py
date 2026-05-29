import logging
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from invoices.models import SalesInvoice, SalesInvoiceItem, AppliedTax, SalesInvoiceStatus
from sales.models import DeliveryOrder, DeliveryOrderItem
from configurator.models import ExportTemplateKind, ConfigurationExportTemplate
from configurator.services.export.service import ConfigurationExportService
from invoices.constants import GENERIC_PRODUCT_TEMPLATE_LABEL
from invoices.services.sales_invoice_payment import resolve_sales_invoice_payment_method

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
        
        # Extract pricing information from the configuration's breakdown
        pricing_breakdown = configuration.pricing_breakdown or {}
        pre_discount_subtotal = Decimal(pricing_breakdown.get('pre_discount_subtotal', str(configuration.subtotal_price.amount)))
        discount_total = abs(Decimal(pricing_breakdown.get('discount_total', '0.00')))
        
        # Calculate discount percentage to support the model's auto-calculation logic
        discount_percentage = Decimal("0.00")
        if pre_discount_subtotal > 0:
            discount_percentage = (discount_total / pre_discount_subtotal) * Decimal("100")
        
        client = configuration.client if hasattr(configuration, "client") else None
        if client is None and client_id:
            from sales.models import Client
            client = Client.objects.filter(pk=client_id).first()

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
            tax_amount=configuration.total_price - configuration.subtotal_price,
            payment_method=resolve_sales_invoice_payment_method(client=client),
        )

        SalesInvoiceItem.objects.create(
            invoice=sales_invoice,
            description=str(configuration.product.description or configuration.product.name),
            quantity=Decimal("1.000"),
            unit_price=pre_discount_subtotal,
            discount_percentage=discount_percentage,
            # discount_amount and line_total are auto-calculated in model's save()
        )

        cls._copy_taxes(configuration, sales_invoice)
        
        sales_invoice.recalculate_totals()

        cls._create_delivery_order(sales_invoice)
        
        return sales_invoice

    @classmethod
    @transaction.atomic
    def finalize_invoice(cls, sales_invoice, send_email=True):
        """
        Converts Pro Forma to Final, assigns invoice number.
        """
        if sales_invoice.status == SalesInvoiceStatus.FINAL:
            sales_invoice._finalize_email_status = cls._email_queue_status(
                queued=False, reason="already_final"
            )
            return sales_invoice

        company = sales_invoice.company
        sales_invoice.status = SalesInvoiceStatus.FINAL
        sales_invoice.date_issued = timezone.now().date()
        # Invoices logic
        next_invoice = cls._generate_next_invoice_data(company)
        sales_invoice.invoice_number = next_invoice['number']
        sales_invoice.incremental_id = next_invoice['incremental_id']
        sales_invoice.save(update_fields=['status', 'date_issued', 'invoice_number', 'incremental_id'])

        if send_email:
            sales_invoice._finalize_email_status = cls.queue_client_email(sales_invoice)
        else:
            sales_invoice._finalize_email_status = cls._email_queue_status(
                queued=False, reason="disabled"
            )

        return sales_invoice

    @classmethod
    def _email_queue_status(cls, *, queued, client_email=None, reason=None):
        status = {
            "email_queued": queued,
            "email_skip_reason": reason,
        }
        # Do not emit client_email when unknown — finalize payload merges this dict
        # and would overwrite the invoice's real client email with null.
        if client_email:
            status["client_email"] = client_email
        return status

    @classmethod
    def queue_client_email(cls, sales_invoice, to_email=None):
        """
        Enqueue invoice PDF email to the client (async via Celery).

        Returns metadata for API/UI: email_queued, client_email, email_skip_reason.
        """
        requested_email = (to_email or "").strip()
        if requested_email:
            email = requested_email
        else:
            client = sales_invoice.client
            email = (client.email or "").strip() if client else ""
        if not email:
            return cls._email_queue_status(queued=False, reason="no_client_email")

        from core.tasks import send_invoice_email_task

        try:
            send_invoice_email_task.delay(sales_invoice.id, to_email=email)
        except Exception:
            logger.exception(
                "Failed to queue invoice email for SalesInvoice %s", sales_invoice.pk
            )
            return cls._email_queue_status(
                queued=False, client_email=email, reason="queue_failed"
            )

        return cls._email_queue_status(queued=True, client_email=email)

    @classmethod
    def get_finalize_email_status(cls, sales_invoice):
        return getattr(
            sales_invoice,
            "_finalize_email_status",
            cls._email_queue_status(queued=False, reason=None),
        )

    @classmethod
    @transaction.atomic
    def duplicate_invoice(cls, sales_invoice):
        """
        Creates a copy of an existing SalesInvoice and its line items.
        The new invoice will be in PROFORMA status without an invoice number.
        """
        original_items = list(sales_invoice.items.all())
        
        # Create the new invoice (Pro Forma)
        new_invoice = SalesInvoice.objects.create(
            company=sales_invoice.company,
            status=SalesInvoiceStatus.PROFORMA,
            invoice_number=None,
            incremental_id=None,
            date_issued=timezone.now().date(),
            due_date=None,
            client=sales_invoice.client,
            configuration=sales_invoice.configuration,
            subtotal_amount=sales_invoice.subtotal_amount,
            tax_amount=sales_invoice.tax_amount,
            total_amount=sales_invoice.total_amount,
            payment_method=sales_invoice.payment_method,
            notes=f"Copy of {sales_invoice.invoice_number or f'Pro Forma #{sales_invoice.id}'}"
        )
        
        # Copy items
        for item in original_items:
            SalesInvoiceItem.objects.create(
                invoice=new_invoice,
                description=item.description,
                quantity=item.quantity,
                unit_price=item.unit_price,
                discount_percentage=item.discount_percentage,
            )
            
        # Copy taxes
        cls._copy_taxes(sales_invoice, new_invoice)
        
        # Ensure totals are correct (should be same as source initially)
        new_invoice.recalculate_totals()
        
        return new_invoice

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

        DeliveryOrderItem.objects.create(
            delivery_order=delivery_order,
            description=str(configuration.product),
            quantity=Decimal("1.000")
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
            kind=ExportTemplateKind.SALES_INVOICE,
            company=sales_invoice.company
        )
        
        if not template:
            product_label = (
                sales_invoice.configuration.product
                if sales_invoice.configuration
                else GENERIC_PRODUCT_TEMPLATE_LABEL
            )
            logger.warning("No Sales Invoice template found for product %s", product_label)
            return None

        return ConfigurationExportService.render_html(sales_invoice, template)
