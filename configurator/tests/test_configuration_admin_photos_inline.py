"""The reception photos inline must only show up for Configurations whose
product is excluded from invoicing (reception-style) — otherwise every
Configuration in the system would show an irrelevant empty "Photos" tab."""
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from configurator.admin.configuration import AssetReceptionPhotoInline, ConfigurationAdmin
from configurator.models import Configuration, ConfigurationStatus, ProductTemplate
from company.models import Company

User = get_user_model()


class ConfigurationAdminPhotosInlineTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Taller Co")
        cls.superuser = User.objects.create_superuser(username="admin", password="x", email="a@a.com")
        cls.flagged_product = ProductTemplate.objects.create(
            name="Recepción de vehículo", company=cls.company,
            exclude_from_invoicing=True,
        )
        cls.plain_product = ProductTemplate.objects.create(
            name="Otra plantilla", company=cls.company,
        )

    def test_inline_present_for_flagged_product(self):
        from django.contrib import admin as django_admin

        config = Configuration.objects.create(
            company=self.company, product=self.flagged_product, status=ConfigurationStatus.SHOP,
        )
        request = RequestFactory().get("/")
        request.user = self.superuser
        admin_instance = ConfigurationAdmin(Configuration, django_admin.site)

        instances = admin_instance.get_inline_instances(request, config)
        self.assertTrue(any(isinstance(i, AssetReceptionPhotoInline) for i in instances))

    def test_inline_absent_for_unflagged_product(self):
        from django.contrib import admin as django_admin

        config = Configuration.objects.create(
            company=self.company, product=self.plain_product, status=ConfigurationStatus.DRAFT,
        )
        request = RequestFactory().get("/")
        request.user = self.superuser
        admin_instance = ConfigurationAdmin(Configuration, django_admin.site)

        instances = admin_instance.get_inline_instances(request, config)
        self.assertFalse(any(isinstance(i, AssetReceptionPhotoInline) for i in instances))

    def test_inline_absent_on_add_form(self):
        from django.contrib import admin as django_admin

        request = RequestFactory().get("/")
        request.user = self.superuser
        admin_instance = ConfigurationAdmin(Configuration, django_admin.site)

        instances = admin_instance.get_inline_instances(request, None)
        self.assertFalse(any(isinstance(i, AssetReceptionPhotoInline) for i in instances))
