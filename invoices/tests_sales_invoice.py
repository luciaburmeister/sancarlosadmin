from django.test import TestCase
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from company.models import Company
from sales.models import Client
from configurator.models import Configuration, ProductTemplate, ConfigurationStatus, ConfigurationItem
from materials.models import Component, ComponentType
from invoices.models import SalesInvoice, SalesInvoiceItem, AppliedTax, TaxType, SalesInvoiceStatus
from invoices.services.sales_invoice_service import SalesInvoiceService
from decimal import Decimal

class SalesInvoiceTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Company")
        self.client = Client.objects.create(name="Test Client", company=self.company)
        self.product = ProductTemplate.objects.create(name="Test Product", company=self.company)
        self.configuration = Configuration.objects.create(
            name="Test Config",
            company=self.company,
            product=self.product,
            client=self.client,
            status=ConfigurationStatus.COMPLETED,
            subtotal_price=100,
            total_price=121
        )
        self.component = Component.objects.create(
            name="Test Component",
            company=self.company,
            component_type=ComponentType.STANDARD,
            unit_price_expression="100"
        )
        self.item = ConfigurationItem.objects.create(
            configuration=self.configuration,
            component=self.component,
            quantity=1,
            unit_price=100,
            is_template_item=True
        )
        
        # Add a tax
        self.tax_type, _ = TaxType.objects.get_or_create(tax_name="VAT", country="ES", defaults={"behavior": "add"})
        self.applied_tax = AppliedTax.objects.create(
            content_object=self.configuration,
            tax_type=self.tax_type,
            selected_percentage=21,
            base_amount=100,
            calculated_amount=21
        )

    def test_invoice_generation_proforma(self):
        invoice = SalesInvoiceService.create_from_configuration(self.configuration)
        
        self.assertEqual(invoice.status, SalesInvoiceStatus.PROFORMA)
        self.assertIsNone(invoice.invoice_number)
        self.assertEqual(invoice.total_amount.amount, Decimal('121.00'))
        self.assertEqual(invoice.items.count(), 1)
        
    def test_finalize_invoice(self):
        from sales.models import DeliveryOrder
        invoice = SalesInvoiceService.create_from_configuration(self.configuration)
        
        # Finalize
        SalesInvoiceService.finalize_invoice(invoice)
        
        self.assertEqual(invoice.status, SalesInvoiceStatus.FINAL)
        self.assertEqual(invoice.invoice_number, f"INV-{timezone.now().year}-00001")
        self.assertEqual(invoice.incremental_id, 1)
        
        # Check Delivery Order
        do = DeliveryOrder.objects.get(sales_invoice=invoice)
        self.assertEqual(do.number, f"DO-{timezone.now().year}-00001")
        self.assertEqual(do.incremental_id, 1)
        self.assertEqual(do.items.count(), 1)

    def test_sequential_numbering_finalized(self):
        inv1 = SalesInvoiceService.create_from_configuration(self.configuration)
        SalesInvoiceService.finalize_invoice(inv1)
        
        inv2 = SalesInvoiceService.create_from_configuration(self.configuration)
        SalesInvoiceService.finalize_invoice(inv2)
        
        self.assertEqual(inv1.invoice_number, f"INV-{timezone.now().year}-00001")
        self.assertEqual(inv2.invoice_number, f"INV-{timezone.now().year}-00002")
        self.assertEqual(inv1.incremental_id, 1)
        self.assertEqual(inv2.incremental_id, 2)

    def test_sequential_tax_calculation(self):
        # Create a new config with a specific subtotal
        config = Configuration.objects.create(
            name="Sequential Tax Config",
            company=self.company,
            product=self.product,
            client=self.client,
            status=ConfigurationStatus.COMPLETED,
            subtotal_price=100,
            total_price=121 # 100 + 10 (10%) + 11 (10% on 110)
        )
        Component.objects.create(
            name="Sequential Component",
            company=self.company,
            component_type=ComponentType.STANDARD,
            unit_price_expression="100"
        )
        item = ConfigurationItem.objects.create(
            configuration=config,
            component=Component.objects.get(name="Sequential Component"),
            quantity=1,
            unit_price=100,
            is_template_item=True
        )
        # Add two 10% taxes
        tax_type1, _ = TaxType.objects.get_or_create(tax_name="Tax1", country="ES", defaults={"behavior": "add"})
        AppliedTax.objects.create(
            content_object=config,
            tax_type=tax_type1,
            selected_percentage=10,
            base_amount=100,
            calculated_amount=10,
            order=1
        )
        tax_type2, _ = TaxType.objects.get_or_create(tax_name="Tax2", country="ES", defaults={"behavior": "add"})
        AppliedTax.objects.create(
            content_object=config,
            tax_type=tax_type2,
            selected_percentage=10,
            base_amount=110,
            calculated_amount=11,
            order=2
        )
        
        invoice = SalesInvoiceService.create_from_configuration(config)
        
        # Check subtotal
        self.assertEqual(invoice.subtotal_amount.amount, Decimal('100.00'))
        
        # Check taxes on invoice
        invoice_ct = ContentType.objects.get_for_model(invoice)
        taxes = list(AppliedTax.objects.filter(content_type=invoice_ct, object_id=invoice.id).order_by('order'))
        self.assertEqual(len(taxes), 2)
        
        # Tax 1
        self.assertEqual(taxes[0].selected_percentage, 10)
        self.assertEqual(taxes[0].base_amount.amount, Decimal('100.00'))
        self.assertEqual(taxes[0].calculated_amount.amount, Decimal('10.00'))
        
        # Tax 2 (Sequential: base should be 110)
        self.assertEqual(taxes[1].selected_percentage, 10)
        self.assertEqual(taxes[1].base_amount.amount, Decimal('110.00'))
        self.assertEqual(taxes[1].calculated_amount.amount, Decimal('11.00'))
        
        # Total
        self.assertEqual(invoice.total_amount.amount, Decimal('121.00'))
        self.assertEqual(invoice.tax_amount.amount, Decimal('21.00'))

    def test_subtotal_preservation(self):
        # Create a config where subtotal_price (120) != sum of line items (100)
        # This simulates a surcharge rule of 20
        config = Configuration.objects.create(
            name="Surcharge Config",
            company=self.company,
            product=self.product,
            client=self.client,
            status=ConfigurationStatus.COMPLETED,
            subtotal_price=120,
            total_price=120 # No taxes for simplicity
        )
        ConfigurationItem.objects.create(
            configuration=config,
            component=self.component,
            quantity=1,
            unit_price=100,
            is_template_item=True
        )
        
        invoice = SalesInvoiceService.create_from_configuration(config)
        
        # Subtotal should be 120 (from config), NOT 100 (from line item)
        self.assertEqual(invoice.subtotal_amount.amount, Decimal('120.00'))
        self.assertEqual(invoice.total_amount.amount, Decimal('120.00'))
        
        # Verify it stays 120 after explicit recalculation
        invoice.recalculate_totals()
        self.assertEqual(invoice.subtotal_amount.amount, Decimal('120.00'))
