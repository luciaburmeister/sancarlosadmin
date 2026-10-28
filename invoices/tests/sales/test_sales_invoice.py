from django.test import TestCase, override_settings
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType
from company.models import Company
from sales.models import Client
from configurator.models import Configuration, ProductTemplate, ConfigurationStatus, ConfigurationItem
from materials.models import Component, ComponentType
from invoices.models import SalesInvoice, SalesInvoiceItem, AppliedTax, TaxType, SalesInvoiceStatus
from invoices.services.sales_invoice_service import SalesInvoiceService
from decimal import Decimal

@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
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
        self.assertEqual(invoice.incremental_id, 1)
        
        # Check Delivery Order
        do = DeliveryOrder.objects.get(sales_invoice=invoice)
        self.assertEqual(do.number, f"DO-{timezone.now().year}-00001")
        self.assertEqual(do.incremental_id, 1)
        self.assertEqual(do.items.count(), 1)

    def test_finalize_sets_date_issued_to_today(self):
        """An old proforma finalized today must carry today's issue date."""
        from datetime import date, timedelta

        invoice = SalesInvoiceService.create_from_configuration(self.configuration)
        old_date = date.today() - timedelta(days=10)
        invoice.date_issued = old_date
        invoice.save(update_fields=["date_issued"])

        SalesInvoiceService.finalize_invoice(invoice)

        self.assertEqual(invoice.date_issued, timezone.localdate())

    def test_finalize_recomputes_automatic_due_date_from_new_issue_date(self):
        """A due date still derived from the draft's issue date follows the
        issue date set at finalize, including the client's payment days."""
        from datetime import date, timedelta
        from invoices.due_dates import compute_due_date

        self.client.payment_terms_days = 30
        self.client.payment_days = [25]
        self.client.save()
        invoice = SalesInvoiceService.create_from_configuration(self.configuration)
        old_date = date.today() - timedelta(days=40)
        invoice.date_issued = old_date
        invoice.due_date = compute_due_date(old_date, 30, [25])
        invoice.save(update_fields=["date_issued", "due_date"])

        SalesInvoiceService.finalize_invoice(invoice)

        invoice.refresh_from_db()
        self.assertEqual(invoice.due_date, compute_due_date(timezone.localdate(), 30, [25]))

    def test_finalize_keeps_due_date_set_by_hand(self):
        from datetime import date, timedelta

        invoice = SalesInvoiceService.create_from_configuration(self.configuration)
        manual = date.today() + timedelta(days=90)
        invoice.date_issued = date.today() - timedelta(days=10)
        invoice.due_date = manual
        invoice.save(update_fields=["date_issued", "due_date"])

        SalesInvoiceService.finalize_invoice(invoice)

        invoice.refresh_from_db()
        self.assertEqual(invoice.due_date, manual)

    def test_refinalize_keeps_original_date_issued(self):
        """Finalizing an already-final invoice must not touch its issue date."""
        from datetime import date, timedelta

        invoice = SalesInvoiceService.create_from_configuration(self.configuration)
        SalesInvoiceService.finalize_invoice(invoice)
        old_date = date.today() - timedelta(days=5)
        invoice.date_issued = old_date
        invoice.save(update_fields=["date_issued"])

        SalesInvoiceService.finalize_invoice(invoice)

        invoice.refresh_from_db()
        self.assertEqual(invoice.date_issued, old_date)

    def test_sequential_numbering_finalized(self):
        inv1 = SalesInvoiceService.create_from_configuration(self.configuration)
        SalesInvoiceService.finalize_invoice(inv1)
        
        inv2 = SalesInvoiceService.create_from_configuration(self.configuration)
        SalesInvoiceService.finalize_invoice(inv2)
        
        self.assertEqual(inv1.invoice_number, f"INV-{timezone.now().year}-00001")
        self.assertEqual(inv2.invoice_number, f"INV-{timezone.now().year}-00002")
        self.assertEqual(inv1.incremental_id, 1)
        self.assertEqual(inv2.incremental_id, 2)

    def test_additive_tax_calculation(self):
        # Create a new config with a specific subtotal
        config = Configuration.objects.create(
            name="Additive Tax Config",
            company=self.company,
            product=self.product,
            client=self.client,
            status=ConfigurationStatus.COMPLETED,
            subtotal_price=100,
            total_price=120 # 100 + 10 (10% on 100) + 10 (10% on 100)
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
            base_amount=100,
            calculated_amount=10,
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
        
        # Tax 2 (Now additive: base should be 100, magnitude 10)
        self.assertEqual(taxes[1].selected_percentage, 10)
        self.assertEqual(taxes[1].base_amount.amount, Decimal('100.00'))
        self.assertEqual(taxes[1].calculated_amount.amount, Decimal('10.00'))
        
        # Total
        self.assertEqual(invoice.total_amount.amount, Decimal('120.00'))
        self.assertEqual(invoice.tax_amount.amount, Decimal('20.00'))

    def test_subtotal_preservation(self):
        # Create a config where subtotal_price (120) != sum of line items (100)
        # This simulates a surcharge/markup rule of 20
        config = Configuration.objects.create(
            name="Surcharge Config",
            company=self.company,
            product=self.product,
            client=self.client,
            status=ConfigurationStatus.COMPLETED,
            subtotal_price=120,
            total_price=120 # No taxes for simplicity
        )
        # The configuration has items that only sum to 100 (e.g. material cost)
        ConfigurationItem.objects.create(
            configuration=config,
            component=self.component,
            quantity=1,
            unit_price=100,
            is_template_item=True
        )
        
        invoice = SalesInvoiceService.create_from_configuration(config)
        
        # Subtotal should be 120 (from config), NOT 100 (from line item summation)
        self.assertEqual(invoice.subtotal_amount.amount, Decimal('120.00'))
        self.assertEqual(invoice.total_amount.amount, Decimal('120.00'))
        
        # Verify it stays 120 after explicit recalculation (it should NOT sum the items)
        invoice.recalculate_totals()
        self.assertEqual(invoice.subtotal_amount.amount, Decimal('120.00'))

    def test_line_item_discount(self):
        # Create a config with a discount in breakdown
        config = Configuration.objects.create(
            name="Discount Config",
            company=self.company,
            product=self.product,
            client=self.client,
            status=ConfigurationStatus.COMPLETED,
            subtotal_price=80,
            total_price=80,
            pricing_breakdown={
                "pre_discount_subtotal": "100.00",
                "discount_total": "-20.00",
                "post_discount_subtotal": "80.00"
            }
        )
        
        invoice = SalesInvoiceService.create_from_configuration(config)
        
        self.assertEqual(invoice.items.count(), 1)
        item = invoice.items.first()
        
        self.assertEqual(item.unit_price.amount, Decimal('100.00'))
        self.assertEqual(item.discount_amount.amount, Decimal('20.00'))
        self.assertEqual(item.line_total.amount, Decimal('80.00'))
        self.assertEqual(invoice.subtotal_amount.amount, Decimal('80.00'))

    def test_duplicate_invoice(self):
        # Create an invoice with an item having a discount
        invoice = SalesInvoice.objects.create(
            company=self.company,
            client=self.client,
            subtotal_amount=80,
            total_amount=80
        )
        SalesInvoiceItem.objects.create(
            invoice=invoice,
            description="Test Item",
            quantity=1,
            unit_price=100,
            discount_percentage=20,
            discount_amount=20,
            line_total=80
        )
        
        # Duplicate
        new_invoice = SalesInvoiceService.duplicate_invoice(invoice)
        
        self.assertEqual(new_invoice.status, SalesInvoiceStatus.PROFORMA)
        self.assertIsNone(new_invoice.invoice_number)
        self.assertEqual(new_invoice.subtotal_amount.amount, Decimal('80.00'))
        self.assertEqual(new_invoice.items.count(), 1)
        
        new_item = new_invoice.items.first()
        self.assertEqual(new_item.description, "Test Item")
        self.assertEqual(new_item.unit_price.amount, Decimal('100.00'))
        self.assertEqual(new_item.discount_amount.amount, Decimal('20.00'))
        self.assertEqual(new_item.line_total.amount, Decimal('80.00'))


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class InvoiceNumberFormatTest(TestCase):
    """Tests for Company.invoice_number_format on invoice generation."""

    def setUp(self):
        self.company = Company.objects.create(name="Format Test Co")
        self.client = Client.objects.create(name="Test Client", company=self.company)
        self.product = ProductTemplate.objects.create(name="Test Product", company=self.company)
        self.component = Component.objects.create(
            name="Test Component",
            company=self.company,
            component_type=ComponentType.STANDARD,
            unit_price_expression="100",
        )
        self.tax_type, _ = TaxType.objects.get_or_create(
            tax_name="VAT", country="ES", defaults={"behavior": "add"}
        )

    def _make_config(self):
        config = Configuration.objects.create(
            name="Fmt Config",
            company=self.company,
            product=self.product,
            client=self.client,
            status=ConfigurationStatus.COMPLETED,
            subtotal_price=100,
            total_price=121,
        )
        ConfigurationItem.objects.create(
            configuration=config,
            component=self.component,
            quantity=1,
            unit_price=100,
            is_template_item=True,
        )
        AppliedTax.objects.create(
            content_object=config,
            tax_type=self.tax_type,
            selected_percentage=21,
            base_amount=100,
            calculated_amount=21,
        )
        return config

    def test_number_follows_company_format_and_keeps_counting(self):
        """The company format only renders the number; the per-company
        sequence keeps counting whatever the format, and no format or an
        empty one falls back to INV-{year}-{number}."""
        year = timezone.now().year
        cases = [
            (None, f"INV-{year}-00001"),
            ("FC-{year}/{number}", f"FC-{year}/00002"),
            ("", f"INV-{year}-00003"),
            ("{year}-FX-{number}", f"{year}-FX-00004"),
            ("{year}-FX-{number}", f"{year}-FX-00005"),
        ]
        for fmt, expected in cases:
            with self.subTest(format=fmt):
                if fmt is not None:
                    self.company.invoice_number_format = fmt
                    self.company.save(update_fields=["invoice_number_format"])

                invoice = SalesInvoiceService.create_from_configuration(self._make_config())
                SalesInvoiceService.finalize_invoice(invoice)

                self.assertEqual(invoice.invoice_number, expected)


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class ProformaNumberTest(TestCase):
    """Proforma numbers: own per-company sequence, kept after finalizing."""

    def setUp(self):
        self.company = Company.objects.create(name="Proforma Co")
        self.client = Client.objects.create(name="Test Client", company=self.company)
        self.year = timezone.now().year

    def _make_invoice(self, company=None, **kwargs):
        company = company or self.company
        return SalesInvoice.objects.create(company=company, client=self.client, **kwargs)

    def test_assigned_on_creation(self):
        invoice = self._make_invoice()

        self.assertEqual(invoice.proforma_number, f"PF-{self.year}-00001")
        self.assertEqual(invoice.proforma_incremental_id, 1)
        self.assertIsNone(invoice.invoice_number)
        self.assertEqual(invoice.display_number, f"PF-{self.year}-00001")

    def test_sequential_per_company(self):
        other = Company.objects.create(name="Other Co")
        first = self._make_invoice()
        second = self._make_invoice()
        other_first = self._make_invoice(company=other)

        self.assertEqual(first.proforma_incremental_id, 1)
        self.assertEqual(second.proforma_incremental_id, 2)
        self.assertEqual(other_first.proforma_incremental_id, 1)

    def test_custom_format(self):
        self.company.proforma_number_format = "PRO/{year}/{number}"
        self.company.save(update_fields=["proforma_number_format"])

        invoice = self._make_invoice()

        self.assertEqual(invoice.proforma_number, f"PRO/{self.year}/00001")

    def test_kept_after_finalize_and_independent_of_invoice_sequence(self):
        self._make_invoice()  # a proforma that is never finalized
        invoice = self._make_invoice()

        SalesInvoiceService.finalize_invoice(invoice, send_email=False)
        invoice.refresh_from_db()

        self.assertEqual(invoice.proforma_number, f"PF-{self.year}-00002")
        self.assertEqual(invoice.invoice_number, f"INV-{self.year}-00001")
        self.assertEqual(invoice.display_number, f"INV-{self.year}-00001")

    def test_resave_does_not_renumber(self):
        invoice = self._make_invoice()
        invoice.notes = "edited"
        invoice.save()
        invoice.refresh_from_db()

        self.assertEqual(invoice.proforma_incremental_id, 1)

    def test_external_invoice_gets_no_proforma_number(self):
        invoice = self._make_invoice(is_external=True, invoice_number="EXT-1")

        self.assertIsNone(invoice.proforma_number)
        self.assertEqual(invoice.display_number, "EXT-1")
        # External rows don't consume the sequence.
        self.assertEqual(self._make_invoice().proforma_incremental_id, 1)

    def test_duplicate_gets_new_proforma_number(self):
        original = self._make_invoice()
        copy = SalesInvoiceService.duplicate_invoice(original)

        self.assertEqual(copy.proforma_incremental_id, 2)
        self.assertEqual(copy.notes, f"Copy of PF-{self.year}-00001")
