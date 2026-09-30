"""
The Verifacti create payload (VerifactiProvider._build_create_payload).

'lineas' is the invoice's tax breakdown: one entry per tax and rate, built
from the invoice-level tax rows, so the header discount and any advance
deductions are already netted out of each base and quota.

- the recargo de equivalencia rides on its IVA entry (clave_regimen 18);
- retentions (IRPF) are never sent;
- any other tax that is neither IGIC nor IPSI goes as IVA at its rate;
- the part of the taxable base no tax covers is sent as not subject (N1);
- importe_total is Σ bases + Σ IVA + Σ recargo, never reduced by a retention.
"""
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import SimpleTestCase, TestCase, override_settings
from djmoney.money import Money

from apps.einvoicing.models import EInvoicingAccount
from apps.einvoicing.providers.verifacti import VerifactiProvider
from company.models import Company
from inventory.models import InventoryItem
from invoices.models import (
    AppliedTax,
    SalesInvoice,
    SalesInvoiceItem,
    SalesInvoiceStatus,
    TaxType,
)
from invoices.services.advances import AdvanceDeductionService, AdvanceInvoiceService
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from invoices.services.sales_invoice_service import SalesInvoiceService
from invoices.tests.advance_fixtures import AdvanceExpedienteMixin, UserExpedienteMixin
from sales.models import Client, QuoteInventoryItem

AMOUNT_FIELDS = ("base_imponible", "cuota_repercutida", "cuota_recargo_equivalencia")


def _iva(base, pct, cuota):
    """A 'lineas' entry at an IVA rate, general regime."""
    return {
        "impuesto": "01",
        "calificacion_operacion": "S1",
        "base_imponible": base,
        "tipo_impositivo": pct,
        "cuota_repercutida": cuota,
    }


class PayloadMixin:
    def payload(self, invoice):
        """The create payload of ``invoice``; checks importe_total is
        Σ bases + Σ IVA + Σ recargo of its 'lineas'."""
        invoice.refresh_from_db()
        account = EInvoicingAccount(company=invoice.company, name="Verifacti", invoice_serie="INV")
        payload = VerifactiProvider()._build_create_payload(account, invoice)
        self.assertEqual(
            Decimal(payload["importe_total"]),
            sum(
                Decimal(linea[field])
                for linea in payload["lineas"] for field in AMOUNT_FIELDS if field in linea
            ),
        )
        return payload

    def issue(self, invoice):
        """Finalize the pro forma ``invoice``: its totals are recalculated first."""
        return SalesInvoiceService.finalize_invoice(invoice, send_email=False)


class ManualInvoiceMixin(PayloadMixin):
    """Invoices laid out straight through the ORM: lines, then invoice-level
    tax rows, totals by recalculate_totals, then issued."""

    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(name="Aluminios SL")
        self.client_obj = Client.objects.create(
            name="Cliente SA", company=self.company, tax_identifier="B12345678",
        )
        self.iva = self.tax_type("IVA")

    def tax_type(self, name, behavior="add", **extra):
        tax_type, _created = TaxType.objects.get_or_create(
            tax_name=name, country="ES", defaults={"behavior": behavior, **extra},
        )
        return tax_type

    def make_invoice(self, lines, taxes, discount=Decimal("0.00")):
        """``lines``: [(description, quantity, unit price)]; ``taxes``:
        [(tax type, %, base)], a base of None meaning the whole taxable base."""
        invoice = SalesInvoice.objects.create(
            company=self.company,
            client=self.client_obj,
            date_issued=date(2026, 9, 29),
            discount_amount=Money(discount, "EUR"),
        )
        for description, quantity, unit_price in lines:
            SalesInvoiceItem.objects.create(
                invoice=invoice, description=description,
                quantity=quantity, unit_price=Money(unit_price, "EUR"),
            )
        invoice_ct = ContentType.objects.get_for_model(SalesInvoice)
        for order, (tax_type, percentage, base) in enumerate(taxes):
            AppliedTax.objects.create(
                content_type=invoice_ct, object_id=invoice.pk, order=order,
                tax_type=tax_type, selected_percentage=percentage, base_amount=base,
            )
        invoice.recalculate_totals()
        invoice.status = SalesInvoiceStatus.FINAL
        invoice.invoice_number = "INV-2026-00001"
        invoice.save(update_fields=["status", "invoice_number"])
        return invoice


class PlainInvoiceTests(ManualInvoiceMixin, TestCase):
    def test_a_single_rate_invoice(self):
        invoice = self.make_invoice(
            [("Ventana corredera", Decimal("2"), Decimal("50.00"))],
            [(self.iva, Decimal("21"), None)],
        )

        self.assertEqual(self.payload(invoice), {
            "serie": "INV",
            "numero": "INV-2026-00001",
            "fecha_expedicion": "29-09-2026",
            "tipo_factura": "F1",
            "descripcion": "Ventana corredera",
            "lineas": [_iva("100.00", "21.00", "21.00")],
            "importe_total": "121.00",
            "nif": "B12345678",
            "nombre": "Cliente SA",
        })
        self.assertEqual(invoice.total_amount.amount, Decimal("121.00"))

    def test_a_credit_note(self):
        invoice = self.make_invoice(
            [("Abono ventana", Decimal("1"), Decimal("-100.00"))],
            [(self.iva, Decimal("21"), None)],
        )

        payload = self.payload(invoice)

        self.assertEqual(payload["lineas"], [_iva("-100.00", "21.00", "-21.00")])
        self.assertEqual(payload["importe_total"], "-121.00")


class GroupedPerRateTests(ManualInvoiceMixin, TestCase):
    def test_one_entry_per_rate_net_of_the_header_discount(self):
        # 1,700.00 of lines, 34.00 of header discount spread over both rates
        invoice = self.make_invoice(
            [
                ("Ventana", Decimal("1"), Decimal("1000.00")),
                ("Persiana", Decimal("1"), Decimal("200.00")),
                ("Toldo", Decimal("1"), Decimal("500.00")),
            ],
            [
                (self.iva, Decimal("21"), Decimal("1176.00")),
                (self.iva, Decimal("10"), Decimal("490.00")),
            ],
            discount=Decimal("34.00"),
        )

        payload = self.payload(invoice)

        self.assertEqual(payload["lineas"], [
            _iva("1176.00", "21.00", "246.96"),
            _iva("490.00", "10.00", "49.00"),
        ])
        self.assertEqual(payload["importe_total"], "1961.96")
        self.assertEqual(invoice.total_amount.amount, Decimal("1961.96"))

    def test_rows_of_one_rate_share_an_entry(self):
        vat = self.tax_type("VAT")
        invoice = self.make_invoice(
            [("Ventana", Decimal("1"), Decimal("600.00")), ("Puerta", Decimal("1"), Decimal("400.00"))],
            [(self.iva, Decimal("21"), Decimal("600.00")), (vat, Decimal("21"), Decimal("400.00"))],
        )

        self.assertEqual(self.payload(invoice)["lineas"], [_iva("1000.00", "21.00", "210.00")])

    def test_igic(self):
        igic = self.tax_type("IGIC")
        invoice = self.make_invoice(
            [("Ventana", Decimal("1"), Decimal("200.00"))], [(igic, Decimal("7"), None)],
        )

        payload = self.payload(invoice)

        self.assertEqual(payload["lineas"], [{
            "impuesto": "03",
            "calificacion_operacion": "S1",
            "base_imponible": "200.00",
            "tipo_impositivo": "7.00",
            "cuota_repercutida": "14.00",
        }])
        self.assertEqual(payload["importe_total"], "214.00")


class RetentionTests(ManualInvoiceMixin, TestCase):
    def test_irpf_is_not_sent_nor_taken_off_the_total(self):
        irpf = self.tax_type("IRPF", behavior="subtract")
        invoice = self.make_invoice(
            [("Dirección de obra", Decimal("1"), Decimal("1000.00"))],
            [(self.iva, Decimal("21"), None), (irpf, Decimal("15"), None)],
        )

        payload = self.payload(invoice)

        self.assertEqual(payload["lineas"], [_iva("1000.00", "21.00", "210.00")])
        self.assertEqual(payload["importe_total"], "1210.00")
        # The invoice itself subtracts the retention
        self.assertEqual(invoice.total_amount.amount, Decimal("1060.00"))

    def test_a_retention_is_recognised_by_its_name_too(self):
        # Even misconfigured as additive, IRPF never goes out as IVA
        irpf = TaxType(tax_name="Retención IRPF", behavior="add")

        self.assertTrue(VerifactiProvider._is_retention(irpf))
        self.assertIsNone(VerifactiProvider._map_tax_key(irpf))


class RecargoTests(ManualInvoiceMixin, TestCase):
    def test_in_the_recargo_fields_of_its_iva_entry(self):
        recargo = self.tax_type("Recargo de Equivalencia")
        invoice = self.make_invoice(
            [("Ventana", Decimal("1"), Decimal("1000.00"))],
            [(self.iva, Decimal("21"), None), (recargo, Decimal("5.2"), None)],
        )

        payload = self.payload(invoice)

        self.assertEqual(payload["lineas"], [{
            **_iva("1000.00", "21.00", "210.00"),
            "clave_regimen": "18",
            "tipo_recargo_equivalencia": "5.20",
            "cuota_recargo_equivalencia": "52.00",
        }])
        self.assertEqual(payload["importe_total"], "1262.00")
        self.assertEqual(invoice.total_amount.amount, Decimal("1262.00"))

    def test_each_recargo_goes_with_the_iva_rate_it_belongs_to(self):
        recargo = self.tax_type("RE", a3_tax_group="RE")
        invoice = self.make_invoice(
            [("Ventana", Decimal("1"), Decimal("1000.00")), ("Toldo", Decimal("1"), Decimal("500.00"))],
            [
                (self.iva, Decimal("21"), Decimal("1000.00")),
                (self.iva, Decimal("10"), Decimal("500.00")),
                (recargo, Decimal("1.4"), Decimal("500.00")),
                (recargo, Decimal("5.2"), Decimal("1000.00")),
            ],
        )

        payload = self.payload(invoice)

        self.assertEqual(payload["lineas"], [
            {
                **_iva("1000.00", "21.00", "210.00"),
                "clave_regimen": "18",
                "tipo_recargo_equivalencia": "5.20",
                "cuota_recargo_equivalencia": "52.00",
            },
            {
                **_iva("500.00", "10.00", "50.00"),
                "clave_regimen": "18",
                "tipo_recargo_equivalencia": "1.40",
                "cuota_recargo_equivalencia": "7.00",
            },
        ])
        self.assertEqual(payload["importe_total"], "1819.00")
        self.assertEqual(invoice.total_amount.amount, Decimal("1819.00"))

    def test_on_part_of_an_iva_base_that_part_is_an_entry_of_its_own(self):
        recargo = self.tax_type("Recargo de Equivalencia")
        invoice = self.make_invoice(
            [("Ventana", Decimal("1"), Decimal("600.00")), ("Montaje", Decimal("1"), Decimal("400.00"))],
            [(self.iva, Decimal("21"), None), (recargo, Decimal("5.2"), Decimal("600.00"))],
        )

        payload = self.payload(invoice)

        self.assertEqual(payload["lineas"], [
            {
                **_iva("600.00", "21.00", "126.00"),
                "clave_regimen": "18",
                "tipo_recargo_equivalencia": "5.20",
                "cuota_recargo_equivalencia": "31.20",
            },
            _iva("400.00", "21.00", "84.00"),
        ])
        self.assertEqual(payload["importe_total"], "1241.20")
        self.assertEqual(invoice.total_amount.amount, Decimal("1241.20"))


class UntaxedTests(ManualInvoiceMixin, TestCase):
    def test_the_untaxed_part_is_sent_as_not_subject(self):
        invoice = self.make_invoice(
            [("Ventana", Decimal("1"), Decimal("1000.00")), ("Tasa", Decimal("1"), Decimal("200.00"))],
            [(self.iva, Decimal("21"), Decimal("1000.00"))],
        )

        payload = self.payload(invoice)

        self.assertEqual(payload["lineas"], [
            _iva("1000.00", "21.00", "210.00"),
            {"calificacion_operacion": "N1", "base_imponible": "200.00"},
        ])
        self.assertEqual(payload["importe_total"], "1410.00")
        self.assertEqual(invoice.total_amount.amount, Decimal("1410.00"))

    def test_an_invoice_without_taxes(self):
        invoice = self.make_invoice([("Tasa", Decimal("1"), Decimal("300.00"))], [])

        payload = self.payload(invoice)

        self.assertEqual(payload["lineas"], [{"calificacion_operacion": "N1", "base_imponible": "300.00"}])
        self.assertEqual(payload["importe_total"], "300.00")

    def test_rounding_cents_are_not_an_untaxed_part(self):
        # Lines of 100.01 in all, taxed on a base rounded line by line to 100.00
        invoice = self.make_invoice(
            [("Ventana", Decimal("1"), Decimal("33.34")), ("Puerta", Decimal("1"), Decimal("66.67"))],
            [(self.iva, Decimal("21"), Decimal("100.00"))],
        )

        self.assertEqual(self.payload(invoice)["lineas"], [_iva("100.00", "21.00", "21.00")])

    def test_an_unknown_tax_goes_as_iva_at_its_rate(self):
        ecotasa = self.tax_type("Ecotasa")
        invoice = self.make_invoice(
            [("Ventana", Decimal("1"), Decimal("100.00")), ("Tasa", Decimal("1"), Decimal("50.00"))],
            [(self.iva, Decimal("21"), Decimal("100.00")), (ecotasa, Decimal("1"), Decimal("50.00"))],
        )

        # Still flagged in the logs, but its base and quota are sent
        with self.assertLogs("apps.einvoicing.providers.verifacti", level="WARNING"):
            payload = self.payload(invoice)

        self.assertEqual(payload["lineas"], [
            _iva("100.00", "21.00", "21.00"),
            _iva("50.00", "1.00", "0.50"),
        ])
        self.assertEqual(payload["importe_total"], "171.50")
        self.assertEqual(invoice.total_amount.amount, Decimal("171.50"))

    def test_an_exempt_tax_goes_as_iva_at_0_not_as_not_subject(self):
        # Reverse charge (inversión del sujeto pasivo) under a 0 % tax that
        # does not say IVA: sent as S1 at 0 %, as the AEAT accepted it before
        exenta = self.tax_type("EXENTA")
        invoice = self.make_invoice(
            [("Chatarra", Decimal("1"), Decimal("562.00"))], [(exenta, Decimal("0"), None)],
        )

        with self.assertLogs("apps.einvoicing.providers.verifacti", level="WARNING"):
            payload = self.payload(invoice)

        self.assertEqual(payload["lineas"], [_iva("562.00", "0.00", "0.00")])
        self.assertEqual(payload["importe_total"], "562.00")


class TaxKeyTests(SimpleTestCase):
    def test_impuesto_codes(self):
        cases = [
            ("IVA", "add", "NONE", "01"),
            ("IVA Reducido", "add", "NONE", "01"),
            ("VAT", "add", "NONE", "01"),
            ("Impuesto general", "add", "IVA", "01"),
            ("IPSI", "add", "NONE", "02"),
            ("IGIC", "add", "NONE", "03"),
            ("IRPF", "subtract", "IRPF", None),
            ("Retención", "subtract", "NONE", None),
            ("Recargo de Equivalencia", "add", "NONE", None),
            ("R.E.", "add", "NONE", None),
            ("EXENTO", "add", "NONE", "01"),
            ("Ecotasa", "add", "NONE", "01"),
        ]
        for name, behavior, a3_group, code in cases:
            with self.subTest(name=name):
                tax_type = TaxType(tax_name=name, behavior=behavior, a3_tax_group=a3_group)
                self.assertEqual(VerifactiProvider._map_tax_key(tax_type), code)


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class UserExpedienteTests(UserExpedienteMixin, PayloadMixin, TestCase):
    """The user's expediente: 1,557.49 of line items at IVA 21 % with a 2 %
    financing discount, taxable base 1,526.34."""

    def goods(self):
        """An invoice for "100 % del pendiente", advances deducted, issued."""
        summary = ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)
        ratio = ProjectFileInvoiceService.compute_invoice_line_ratio(summary, "percentage", Decimal("100"))
        invoice = ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=self.project_file, company=self.company, client=self.client_obj,
            date_issued=date(2026, 9, 29), due_date=None,
            items_data=ProjectFileInvoiceService.build_items_for_ratio(summary, ratio),
        )
        AdvanceDeductionService.apply(invoice)
        return self.issue(invoice)

    def advance(self):
        """A 30 % advance, issued."""
        return self.issue(AdvanceInvoiceService.create(
            self.project_file, date_issued=date(2026, 9, 3), percentage=Decimal("30"),
        ))

    def add_reduced_rate_item(self):
        """400.00 of line items at IVA 10 %, also under the financing rule."""
        stock_item = InventoryItem.objects.create(name="Toldo", company=self.company)
        AppliedTax.objects.create(
            content_type=ContentType.objects.get_for_model(InventoryItem),
            object_id=stock_item.pk,
            tax_type=self.iva,
            selected_percentage=Decimal("10"),
        )
        QuoteInventoryItem.objects.create(
            quote=self.quote, inventory_item=stock_item,
            quantity=Decimal("1"), unit_price=Decimal("400.00"),
        )
        self.quote.recalc_totals()

    def test_the_header_discount_is_netted_out(self):
        invoice = self.goods()

        payload = self.payload(invoice)

        # Not the 1,557.49 of the lines
        self.assertEqual(payload["lineas"], [_iva("1526.34", "21.00", "320.53")])
        self.assertEqual(payload["importe_total"], "1846.87")
        self.assertEqual(invoice.total_amount.amount, Decimal("1846.87"))

    def test_advance_and_rest_with_the_discount_and_the_deduction(self):
        advance = self.payload(self.advance())
        rest = self.payload(self.goods())

        self.assertEqual(advance["tipo_factura"], "F1")
        self.assertEqual(advance["lineas"], [_iva("457.90", "21.00", "96.16")])
        self.assertEqual(advance["importe_total"], "554.06")
        self.assertEqual(rest["tipo_factura"], "F1")
        self.assertEqual(rest["lineas"], [_iva("1068.44", "21.00", "224.37")])
        self.assertEqual(rest["importe_total"], "1292.81")
        # One invoice for everything
        self.assertEqual(
            Decimal(advance["importe_total"]) + Decimal(rest["importe_total"]), Decimal("1846.87"),
        )

    def test_two_rates_with_the_discount_and_the_deduction(self):
        # Taxable base per rate: 1,526.34 at 21 % and 392.00 at 10 %
        self.add_reduced_rate_item()

        advance = self.payload(self.advance())
        rest_invoice = self.goods()
        rest = self.payload(rest_invoice)

        self.assertCountEqual(advance["lineas"], [
            _iva("457.90", "21.00", "96.16"),
            _iva("117.60", "10.00", "11.76"),
        ])
        self.assertEqual(advance["importe_total"], "683.42")
        self.assertCountEqual(rest["lineas"], [
            _iva("1068.44", "21.00", "224.37"),
            _iva("274.40", "10.00", "27.44"),
        ])
        self.assertEqual(rest["importe_total"], "1594.65")
        self.assertEqual(rest_invoice.total_amount.amount, Decimal("1594.65"))
        # One invoice for everything: 1,918.34 + 320.53 + 39.20
        self.assertEqual(
            Decimal(advance["importe_total"]) + Decimal(rest["importe_total"]), Decimal("2278.07"),
        )


@override_settings(AEAT_SUBMISSIONS_ENABLED=False)
class DeductionTests(AdvanceExpedienteMixin, PayloadMixin, TestCase):
    """An expediente of one 1,000.00 configuration at IVA 21 %, no discount."""

    def test_the_deduction_is_netted_out(self):
        self.make_advance(Decimal("300.00"), "30")
        invoice = self.invoice_goods()
        AdvanceDeductionService.apply(invoice)

        payload = self.payload(self.issue(invoice))

        # Not a line of 1,000.00 and another of −300.00
        self.assertEqual(payload["lineas"], [_iva("700.00", "21.00", "147.00")])
        self.assertEqual(payload["importe_total"], "847.00")
