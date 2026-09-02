from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from company.models import Company
from materials.models import Category, Material, Provider


class MaterialExportTests(TestCase):
    """Regression tests for the Material admin export."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="ACME")
        category = Category.objects.create(name="Perfiles", company=cls.company)
        provider = Provider.objects.create(name="Cortizo", company=cls.company)
        Material.objects.create(
            reference_code="M1",
            name="Perfil 40x40",
            category=category,
            provider=provider,
            company=cls.company,
            cost_per_unit=10,
        )
        cls.user = get_user_model().objects.create_superuser(
            username="exporter", email="exporter@example.com", password="secret"
        )

    def test_admin_export_with_selected_fields(self):
        """The admin export form passes extra kwargs down to the widgets.

        MoneyAmountWidget.render() used to reject them, which made every
        export from the Material admin fail with a TypeError.
        """
        self.client.force_login(self.user)
        url = reverse("admin:materials_material_export")

        form = self.client.get(url).context["form"]
        data = {"resource": 0, "format": 0}
        data.update(
            {name: "on" for name in form.fields if name.startswith("materialresource_")}
        )

        response = self.client.post(url, data)

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        body = response.content.decode()
        self.assertIn("M1,Perfil 40x40", body)
        self.assertIn("Cortizo", body)
