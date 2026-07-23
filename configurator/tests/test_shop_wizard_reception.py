"""Shop wizard behavior for reception products (exclude_from_invoicing)."""
from django.test import TestCase, RequestFactory

from assets.models import Asset, AssetType
from company.models import Company
from configurator.forms import ConfigurationForm
from configurator.forms.shop_forms import ProjectFileSelectionForm
from configurator.models import ProductTemplate, ConfigurationStatus
from configurator.views.configuration.shop_wizard_views import (
    ShopConfigurationWizardView,
)
from management.models import ProjectFile
from production.models import ProductionTicket
from sales.models import Client


class ShopWizardReceptionTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.company = Company.objects.create(name="Taller Co")
        self.client_obj = Client.objects.create(name="Acme", company=self.company)
        self.product = ProductTemplate.objects.create(
            name="Recepción de vehículo",
            company=self.company,
            flow_kind="shop",
            exclude_from_invoicing=True,
        )
        self.asset = Asset.objects.create(
            name="Seat Arona",
            asset_type=AssetType.objects.create(name="Vehículo", company=self.company),
            client=self.client_obj,
            company=self.company,
        )
        self.project_file = ProjectFile.objects.create(
            name="Reparación",
            company=self.company,
            client=self.client_obj,
            asset=self.asset,
        )

    def _run_wizard_done(self):
        view = ShopConfigurationWizardView(model_admin=None)
        view.instance = None
        view.product = self.product
        view.request = self.factory.post("/")

        selection_form = ProjectFileSelectionForm(
            data={"project_file": self.project_file.pk}
        )
        self.assertTrue(selection_form.is_valid(), selection_form.errors)
        config_form = ConfigurationForm(data={}, product=self.product)
        self.assertTrue(config_form.is_valid(), config_form.errors)

        return view.done([selection_form, config_form])

    def test_reception_skips_ticket_and_slip_and_links_expediente(self):
        from configurator.models import ReceivalSlip

        response = self._run_wizard_done()

        config = self.project_file.direct_configurations.get()
        self.assertEqual(config.status, ConfigurationStatus.SHOP)
        self.assertEqual(config.asset_id, self.asset.pk)
        self.assertEqual(config.product_id, self.product.pk)

        self.assertEqual(ProductionTicket.objects.count(), 0)
        self.assertEqual(ReceivalSlip.objects.count(), 0)

        # Redirects back to the expediente, not to a receival slip
        self.assertIn(f"projectfile/{self.project_file.pk}", response.url)

    def test_project_file_preselected_from_query_param(self):
        view = ShopConfigurationWizardView(model_admin=None)
        view.instance = None
        view.product = self.product
        view.initial_dict = {}
        view.request = self.factory.get(
            f"/?project_file_id={self.project_file.pk}"
        )
        initial = view.get_form_initial("project_selection")
        self.assertEqual(initial.get("project_file"), self.project_file)
