"""POL-392: picking which pending work entries a "full" invoice bills,
leaving the rest unlocked for a later one. Covers both the service-layer
filter (build_items_for_remaining) and the wizard step that drives it."""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import QueryDict
from django.test import RequestFactory, TestCase
from formtools.wizard.storage import get_storage
from formtools.wizard.views import StepsHelper
from djmoney.money import Money

from invoices.models import SalesInvoice, TaxType
from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
from invoices.models import PaymentMethod
from invoices.tests.factories import (
    apply_tax, create_invoice, make_client, make_company, make_configuration,
    make_product_template, make_project_file,
)
from invoices.views.invoice.project_file_wizard_views import (
    ProjectFileInvoiceWizardView,
    _selects_work_entries,
)
from management.models import WorkEntry, WorkEntryType


class _TwoWorkEntriesBase(TestCase):
    """One project file with two independent 100 € (net) work entries.
    Shared across every test method in each subclass: an invoice created in
    one test only exists within that test's own transaction."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company(name="Taller Co")
        cls.client_obj = make_client(company=cls.company)
        cls.project_file = make_project_file(
            company=cls.company, client=cls.client_obj, name="PF Partes",
        )
        cls.entry_type = WorkEntryType.objects.create(company=cls.company, name="Porte")
        cls.iva21 = TaxType.objects.create(tax_name="IVA", country="ES", percentage_options=[21])

        cls.entry_a = cls._make_entry("Porte A", day=1)
        cls.entry_b = cls._make_entry("Porte B", day=2)

    @classmethod
    def _make_entry(cls, description, day):
        entry = WorkEntry.objects.create(
            company=cls.company,
            project_file=cls.project_file,
            entry_type=cls.entry_type,
            description=description,
            quantity=Decimal("1"),
            unit_price=Money("100.00", "EUR"),
            date=date(2026, 9, day),
        )
        apply_tax(entry, tax_type=cls.iva21, base=Decimal("100.00"), calculated=Decimal("21.00"))
        return entry

    def _summary(self):
        return ProjectFileInvoiceService.get_invoiceable_summary(self.project_file)


class BuildItemsForRemainingSelectionTests(_TwoWorkEntriesBase):
    def test_no_filter_bills_every_pending_entry(self):
        items = ProjectFileInvoiceService.build_items_for_remaining(self._summary())
        self.assertEqual(
            {i["source_id"] for i in items},
            {f"work_entry_{self.entry_a.pk}", f"work_entry_{self.entry_b.pk}"},
        )

    def test_selecting_one_entry_leaves_the_other_out(self):
        items = ProjectFileInvoiceService.build_items_for_remaining(
            self._summary(), work_entry_ids=[self.entry_a.pk],
        )
        self.assertEqual([i["source_id"] for i in items], [f"work_entry_{self.entry_a.pk}"])

    def test_empty_selection_bills_no_work_entry(self):
        items = ProjectFileInvoiceService.build_items_for_remaining(
            self._summary(), work_entry_ids=[],
        )
        self.assertEqual(items, [])


class WizardSelectionStepTests(_TwoWorkEntriesBase):
    """_build_items_initial honors the "work_entries" step's selection."""

    def _view(self, invoice_mode, work_entries_rows=None):
        view = ProjectFileInvoiceWizardView(
            **ProjectFileInvoiceWizardView.get_initkwargs(), model_admin=None,
        )
        view.invoicing_summary = self._summary()
        data_by_step = {
            "setup": {"invoice_mode": invoice_mode, "_calculated_subtotal": Decimal("0.00")},
            "work_entries": work_entries_rows,
        }
        view.get_cleaned_data_for_step = data_by_step.get
        return view

    def test_only_checked_entries_are_prefilled(self):
        view = self._view("full", work_entries_rows=[
            {"work_entry_id": self.entry_a.pk, "selected": True},
            {"work_entry_id": self.entry_b.pk, "selected": False},
        ])
        items = view._build_items_initial()
        self.assertEqual([i["source_id"] for i in items], [f"work_entry_{self.entry_a.pk}"])

    def test_no_work_entries_step_data_bills_everything(self):
        """The step didn't run (e.g. it was skipped): no selection to apply."""
        view = self._view("full", work_entries_rows=None)
        items = view._build_items_initial()
        self.assertEqual(
            {i["source_id"] for i in items},
            {f"work_entry_{self.entry_a.pk}", f"work_entry_{self.entry_b.pk}"},
        )


class SelectsWorkEntriesConditionTests(_TwoWorkEntriesBase):
    def _wizard(self, invoice_mode):
        view = ProjectFileInvoiceWizardView(model_admin=None)
        view.invoicing_summary = self._summary()
        view.get_cleaned_data_for_step = lambda step: {"invoice_mode": invoice_mode}
        return view

    def test_shown_for_full_mode_with_pending_entries(self):
        self.assertTrue(_selects_work_entries(self._wizard("full")))

    def test_hidden_for_percentage_mode(self):
        self.assertFalse(_selects_work_entries(self._wizard("percentage")))

    def test_hidden_once_every_entry_is_locked(self):
        create_invoice(
            project_file=self.project_file,
            items=[
                {
                    "source_id": f"work_entry_{self.entry_a.pk}",
                    "description": "Porte A",
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("100.00"),
                    "discount_percentage": Decimal("0.00"),
                },
                {
                    "source_id": f"work_entry_{self.entry_b.pk}",
                    "description": "Porte B",
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("100.00"),
                    "discount_percentage": Decimal("0.00"),
                },
            ],
            date_issued=date(2026, 9, 10),
        )
        wizard = self._wizard("full")
        self.assertFalse(_selects_work_entries(wizard))


class EndToEndSelectionTests(_TwoWorkEntriesBase):
    """Creating the invoice with a partial selection locks only what it billed."""

    def test_unselected_entry_stays_unlocked(self):
        invoice = create_invoice(
            project_file=self.project_file,
            items=ProjectFileInvoiceService.build_items_for_remaining(
                self._summary(), work_entry_ids=[self.entry_a.pk],
            ),
            date_issued=date(2026, 9, 10),
        )
        self.assertEqual(invoice.subtotal_amount.amount, Decimal("100.00"))

        self.entry_a.refresh_from_db()
        self.entry_b.refresh_from_db()
        self.assertTrue(self.entry_a.is_locked)
        self.assertFalse(self.entry_b.is_locked)

        remaining_summary = self._summary()
        self.assertEqual(remaining_summary["remaining_subtotal"], Decimal("100.00"))


class EmptySelectionGuardTests(_TwoWorkEntriesBase):
    """Unchecking every work entry (with nothing else pending) must not
    silently create a 0 € invoice: done() refuses it before writing."""

    def _request(self):
        request = RequestFactory().post("/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        request._messages = FallbackStorage(request)
        return request

    def test_no_invoice_lines_refuses_without_creating_an_invoice(self):
        view = ProjectFileInvoiceWizardView(model_admin=None)
        view.project_file = self.project_file
        view.request = self._request()
        form_dict = {
            "setup": SimpleNamespace(cleaned_data={"invoice_mode": "full"}),
            "notes": SimpleNamespace(cleaned_data={}),
            "items": [],
        }

        view.done([], form_dict)

        self.assertEqual(SalesInvoice.objects.filter(project_file=self.project_file).count(), 0)


class SkippedWorkEntriesStepTests(_TwoWorkEntriesBase):
    """A full invoice whose selection step is hidden (nothing pending) must
    still reach the items step. formtools raises KeyError if that declared
    step is read after the condition left it out."""

    def test_items_step_builds_when_the_selection_step_is_skipped(self):
        product = make_product_template(company=self.company, name="Door")
        config = make_configuration(
            product=product, project_file=self.project_file, subtotal="50.00", name="Marco",
        )
        create_invoice(
            project_file=self.project_file,
            items=[
                {
                    "source_id": f"work_entry_{self.entry_a.pk}",
                    "description": "Porte A",
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("100.00"),
                    "discount_percentage": Decimal("0.00"),
                },
                {
                    "source_id": f"work_entry_{self.entry_b.pk}",
                    "description": "Porte B",
                    "quantity": Decimal("1"),
                    "unit_price": Decimal("100.00"),
                    "discount_percentage": Decimal("0.00"),
                },
            ],
            date_issued=date(2026, 9, 10),
        )

        view = self._wizard(self._summary())
        posted = QueryDict(mutable=True)
        posted.update({
            "setup-company": str(self.company.pk),
            "setup-date_issued": "2026-10-29",
            "setup-invoice_mode": "full",
            "setup-percentage": "100.00",
            "setup-payment_method": PaymentMethod.TRANSFER,
        })
        setup = view.get_form("setup", data=posted)
        self.assertTrue(setup.is_valid(), setup.errors)
        view.storage.set_step_data("setup", view.process_step(setup))
        view.storage.current_step = "setup"
        for name in list(view.__dict__):
            if name.startswith("_cleaned_data_cache_") or name in ("_resolved_form_list", "_cache_signature"):
                delattr(view, name)

        items = view.get_form("items")

        self.assertEqual(
            [form.initial.get("source_id") for form in items.forms],
            [f"direct_{config.pk}"],
        )

    def _wizard(self, summary):
        request = RequestFactory().get("/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        request.user = SimpleNamespace(is_superuser=True)
        view = ProjectFileInvoiceWizardView(
            **ProjectFileInvoiceWizardView.get_initkwargs(), model_admin=None,
        )
        view.request = request
        view.prefix = view.get_prefix(request)
        view.storage = get_storage(view.storage_name, view.prefix, request)
        view.steps = StepsHelper(view)
        view.project_file = self.project_file
        view.invoicing_summary = summary
        return view
