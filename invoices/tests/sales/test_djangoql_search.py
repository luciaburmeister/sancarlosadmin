from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import pgettext
from djangoql.exceptions import DjangoQLError, DjangoQLSchemaError
from djangoql.queryset import apply_search
from djangoql.serializers import DjangoQLSchemaSerializer
from djmoney.money import Money

from company.models import Company
from general.djangoql import fields as ql_fields
from general.djangoql.fields import TRANSLATION_CONTEXT
from general.djangoql.registry import registered_fields, registered_models
from general.djangoql.schema import RegisteredDjangoQLSchema, autodiscover
from invoices.models import (
    Invoice,
    InvoiceDocumentType,
    InvoiceItem,
    InvoicePaymentStatus,
    SalesInvoice,
    SalesInvoiceItem,
    SalesInvoiceStatus,
)
from materials.models import Provider
from sales.models import Client


class DjangoQLSearchTestBase(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.company = Company.objects.create(name="Fistex", tax_identifier="B11111111")
        self.provider = Provider.objects.create(
            company=self.company, name="Aluminios SA", tax_identifier="A11111111"
        )
        self.provider_without_nif = Provider.objects.create(
            company=self.company, name="Sin NIF SL"
        )
        self.customer = Client.objects.create(
            company=self.company,
            name="Cliente SA",
            tax_identifier="C11111111",
            is_insured=True,
            max_risk=Money("1000.00", "EUR"),
            current_risk=Money("2500.00", "EUR"),
        )

        self.invoice = self._invoice("F-001", due_in_days=30, file="invoices/f-001.pdf")
        InvoiceItem.objects.create(
            invoice=self.invoice,
            description="Perfil aluminio blanco",
            quantity=Decimal("2"),
            unit_price=Money("500.00", "EUR"),
        )
        InvoiceItem.objects.create(
            invoice=self.invoice,
            description="Perfil aluminio negro",
            quantity=Decimal("1"),
            unit_price=Money("210.00", "EUR"),
        )

        self.overdue_invoice = self._invoice("F-002", due_in_days=-5)
        self.credit_note = self._invoice(
            "R-001",
            due_in_days=10,
            provider=self.provider_without_nif,
            document_type=InvoiceDocumentType.CREDIT_NOTE,
        )
        self.invoice_without_provider = self._invoice("F-003", due_in_days=10, provider=None)

        # Lines can only be added while the invoice is a pro forma.
        self.sales_invoice = SalesInvoice.objects.create(
            company=self.company,
            client=self.customer,
            invoice_number="V-001",
            status=SalesInvoiceStatus.PROFORMA,
            date_issued=self.today,
            due_date=self.today - timedelta(days=5),
        )
        SalesInvoiceItem.objects.create(
            invoice=self.sales_invoice,
            description="Ventana corredera",
            quantity=Decimal("1"),
            unit_price=Money("800.00", "EUR"),
        )
        self.sales_invoice.status = SalesInvoiceStatus.FINAL
        self.sales_invoice.save(update_fields=["status"])
        self.proforma = SalesInvoice.objects.create(
            company=self.company,
            client=self.customer,
            invoice_number="V-002",
            status=SalesInvoiceStatus.PROFORMA,
            date_issued=self.today,
            due_date=self.today - timedelta(days=5),
        )

    def _invoice(self, number, due_in_days, provider="default", **kwargs):
        return Invoice.objects.create(
            company=self.company,
            provider=self.provider if provider == "default" else provider,
            invoice_number=number,
            date_issued=self.today,
            due_date=self.today + timedelta(days=due_in_days),
            payment_status=InvoicePaymentStatus.UNPAID,
            total_amount=Money("1210.00", "EUR"),
            **kwargs,
        )

    def search(self, model, query, lang="en"):
        """Return the pks matched by `query`, read in `lang`."""
        with translation.override(lang):
            qs = apply_search(model.objects.all(), query, RegisteredDjangoQLSchema)
            return set(qs.values_list("pk", flat=True))

    def schema_fields(self, model, lang="en"):
        with translation.override(lang):
            schema = DjangoQLSchemaSerializer().serialize(RegisteredDjangoQLSchema(model))
            return schema["models"][RegisteredDjangoQLSchema.model_label(model)]

    def login_admin(self):
        User = get_user_model()
        self.client.force_login(
            User.objects.create_superuser("admin", "admin@test.com", "pass")
        )


class SchemaShapeTest(DjangoQLSearchTestBase):
    def test_only_declared_fields_are_exposed(self):
        fields = self.schema_fields(Invoice)
        self.assertIn("invoice_number", fields)
        self.assertIn("provider", fields)
        for hidden in ("ocr_raw_response", "gemini_account_response", "image_hash", "file"):
            self.assertNotIn(hidden, fields)

    def test_undeclared_field_is_not_searchable(self):
        with self.assertRaises(DjangoQLSchemaError):
            self.search(Invoice, 'image_hash = "abc"')

    def test_unregistered_model_is_rejected(self):
        with self.assertRaises(DjangoQLSchemaError) as ctx:
            RegisteredDjangoQLSchema(ContentType)
        self.assertIn("djangoql_fields", str(ctx.exception))

    def test_choice_fields_offer_completion_options(self):
        fields = self.schema_fields(Invoice)
        self.assertTrue(fields["document_type"]["options"])
        # A plain text field must not: options are fetched unfiltered, which
        # would leak values across companies.
        self.assertFalse(fields["invoice_number"]["options"])


class LocalizedNameTest(DjangoQLSearchTestBase):
    def test_field_names_are_translated(self):
        fields = self.schema_fields(Invoice, lang="es")
        self.assertIn("numero_factura", fields)
        self.assertIn("fecha_vencimiento", fields)
        self.assertIn("proveedor", fields)
        self.assertNotIn("invoice_number", fields)

    def test_same_query_in_both_languages(self):
        english = self.search(Invoice, 'invoice_number ~ "F-00"')
        spanish = self.search(Invoice, 'numero_factura ~ "F-00"', lang="es")
        self.assertEqual(english, spanish)
        self.assertEqual(
            english,
            {self.invoice.pk, self.overdue_invoice.pk, self.invoice_without_provider.pk},
        )

    def test_localized_relation_path(self):
        self.assertEqual(
            self.search(Invoice, 'proveedor.nombre ~ "Aluminios"', lang="es"),
            {self.invoice.pk, self.overdue_invoice.pk},
        )

    def test_localized_relation_compared_to_none(self):
        self.assertEqual(
            self.search(Invoice, "proveedor = None", lang="es"),
            {self.invoice_without_provider.pk},
        )

    def test_unparseable_translation_falls_back_to_english(self):
        with mock.patch.object(ql_fields, "pgettext", return_value="número factura"):
            self.assertEqual(ql_fields.localized_name("invoice_number"), "invoice_number")
        with mock.patch.object(ql_fields, "pgettext", return_value="not"):
            self.assertEqual(ql_fields.localized_name("is_paid"), "is_paid")
        with mock.patch.object(ql_fields, "pgettext", return_value="numero_factura"):
            self.assertEqual(ql_fields.localized_name("invoice_number"), "numero_factura")

    def test_error_message_lists_localized_choices(self):
        with self.assertRaises(DjangoQLSchemaError) as ctx:
            self.search(Invoice, 'nombre = "x"', lang="es")
        self.assertIn("numero_factura", str(ctx.exception))


class InvoiceFlagTest(DjangoQLSearchTestBase):
    def test_is_overdue(self):
        self.assertEqual(
            self.search(Invoice, "is_overdue = True"), {self.overdue_invoice.pk}
        )
        self.assertEqual(
            self.search(Invoice, "vencida = True", lang="es"), {self.overdue_invoice.pk}
        )
        self.assertNotIn(
            self.overdue_invoice.pk, self.search(Invoice, "is_overdue = False")
        )

    def test_invoice_without_due_date_is_overdue(self):
        undated = self._invoice("F-NODATE", due_in_days=10)
        Invoice.objects.filter(pk=undated.pk).update(due_date=None)

        self.assertEqual(
            self.search(Invoice, "is_overdue = True"),
            {self.overdue_invoice.pk, undated.pk},
        )
        self.assertNotIn(undated.pk, self.search(Invoice, "is_overdue = False"))

    def test_paid_invoice_is_not_overdue(self):
        self.overdue_invoice.payment_status = InvoicePaymentStatus.PAID
        self.overdue_invoice.save(update_fields=["payment_status"])
        self.assertEqual(self.search(Invoice, "is_overdue = True"), set())
        self.assertEqual(self.search(Invoice, "is_paid = True"), {self.overdue_invoice.pk})

    def test_is_credit_note(self):
        self.assertEqual(
            self.search(Invoice, "is_credit_note = True"), {self.credit_note.pk}
        )
        self.assertNotIn(
            self.credit_note.pk, self.search(Invoice, "is_credit_note != True")
        )

    def test_has_file(self):
        self.assertEqual(self.search(Invoice, "has_file = True"), {self.invoice.pk})

    def test_items_missing_account(self):
        self.assertEqual(
            self.search(Invoice, "items_missing_account = True"), {self.invoice.pk}
        )

    def test_flag_combined_with_other_conditions(self):
        self.assertEqual(
            self.search(Invoice, 'is_overdue = True and provider.name ~ "Aluminios"'),
            {self.overdue_invoice.pk},
        )


class RelatedFlagTest(DjangoQLSearchTestBase):
    def test_provider_flag_through_relation(self):
        self.assertEqual(
            self.search(Invoice, "provider.has_tax_identifier = False"),
            {self.credit_note.pk},
        )
        self.assertEqual(
            self.search(Invoice, "proveedor.tiene_nif = False", lang="es"),
            {self.credit_note.pk},
        )

    def test_client_flags_from_sales_invoice(self):
        self.assertEqual(
            self.search(SalesInvoice, "client.is_insured = True"),
            {self.sales_invoice.pk, self.proforma.pk},
        )
        self.assertEqual(
            self.search(SalesInvoice, "client.exceeds_risk_limit = True"),
            {self.sales_invoice.pk, self.proforma.pk},
        )
        self.customer.current_risk = Money("10.00", "EUR")
        self.customer.save(update_fields=["current_risk"])
        self.assertEqual(
            self.search(SalesInvoice, "cliente.supera_riesgo = True", lang="es"), set()
        )

    def test_proforma_is_never_overdue(self):
        self.assertEqual(
            self.search(SalesInvoice, "is_overdue = True"), {self.sales_invoice.pk}
        )
        self.assertEqual(
            self.search(SalesInvoice, "is_final = True"), {self.sales_invoice.pk}
        )


class ToManySearchTest(DjangoQLSearchTestBase):
    def test_line_item_search(self):
        self.assertEqual(
            self.search(Invoice, 'items.description ~ "aluminio"'), {self.invoice.pk}
        )
        self.assertEqual(
            self.search(Invoice, 'lineas.descripcion ~ "aluminio"', lang="es"),
            {self.invoice.pk},
        )

    def test_admin_search_does_not_duplicate_rows(self):
        # Both line items match, so the changelist has to de-duplicate. The
        # request runs under the site language (Spanish), which is also the
        # language the field names are read in.
        self.login_admin()
        response = self.client.get(
            reverse("admin:invoices_invoice_changelist"),
            {"q": 'lineas.descripcion ~ "aluminio"', "q-l": "on"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cl"].result_count, 1)

    def test_admin_search_falls_back_to_plain_search_fields(self):
        self.login_admin()
        response = self.client.get(
            reverse("admin:invoices_invoice_changelist"), {"q": "F-001"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cl"].result_count, 1)

    def test_admin_reports_unknown_field_instead_of_crashing(self):
        self.login_admin()
        response = self.client.get(
            reverse("admin:invoices_invoice_changelist"),
            {"q": 'image_hash = "abc"', "q-l": "on"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cl"].result_count, 0)
        self.assertIn(
            "Unknown field: image_hash",
            " ".join(str(m) for m in response.context["messages"]),
        )


class CompletionEndpointTest(DjangoQLSearchTestBase):
    def test_completion_schema_is_served_in_the_site_language(self):
        self.login_admin()
        response = self.client.get(
            reverse("admin:invoices_invoice_djangoql_introspect")
        )
        self.assertEqual(response.status_code, 200)
        fields = response.json()["models"]["invoices.invoice"]
        self.assertIn("numero_factura", fields)
        self.assertIn("vencida", fields)
        self.assertEqual(fields["proveedor"]["relation"], "materials.provider")
        self.assertNotIn("invoice_number", fields)


class TranslationCoverageTest(TestCase):
    """
    Every exposed field name must have a Spanish entry.

    These msgids are built at runtime (`pgettext(context, field_name)`), so
    xgettext cannot see them: a `makemessages` run marks the whole block in
    locale/es/LC_MESSAGES/django.po obsolete and the search UI silently falls
    back to English names. This test is what makes that loud - if it fails
    after running makemessages, restore the "djangoql field" block.
    """

    #: Names that read the same in Spanish, so no entry is expected.
    SAME_IN_SPANISH = {"id", "email", "nima"}

    def test_every_registered_field_name_is_translated(self):
        autodiscover()
        untranslated = set()
        with translation.override("es"):
            for model in registered_models():
                for spec in registered_fields(model):
                    name = spec.name if isinstance(spec, type) else spec
                    if name in self.SAME_IN_SPANISH:
                        continue
                    if pgettext(TRANSLATION_CONTEXT, name) == name:
                        untranslated.add(name)
        self.assertEqual(
            untranslated,
            set(),
            "Missing 'djangoql field' entries in locale/es/LC_MESSAGES/"
            "django.po for: %s" % ", ".join(sorted(untranslated)),
        )


class InvalidQueryTest(DjangoQLSearchTestBase):
    def test_wrong_value_type_is_reported(self):
        with self.assertRaises(DjangoQLSchemaError):
            self.search(Invoice, "invoice_number = 5")

    def test_flag_rejects_unsupported_operator(self):
        try:
            matched = self.search(Invoice, "is_overdue in (True, False)")
        except DjangoQLError as exc:
            # Either the parser or the flag itself refuses it; both are fine as
            # long as the query never runs silently wrong.
            self.assertTrue(str(exc))
        else:
            self.fail("expected the flag to reject 'in', got %r" % matched)
