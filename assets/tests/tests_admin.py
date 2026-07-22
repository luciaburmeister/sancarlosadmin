from django.test import TestCase, RequestFactory
from django.contrib.admin.sites import AdminSite
from django.urls import reverse
from company.models import Company, CompanyUser
from assets.models import Asset, AssetType, AssetIdentifier
from assets.admin.asset import AssetIdentifierInline
from django.contrib.auth import get_user_model

User = get_user_model()

class AssetIdentifierInlineTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.site = AdminSite()
        self.company = Company.objects.create(
            name="Test Company",
            asset_identifier_labels=["VIN", "Plate"]
        )
        self.user = User.objects.create_superuser(username="admin", password="password", email="admin@test.com")
        self.company_user = CompanyUser.objects.create(user=self.user, company=self.company)
        
        self.asset_type = AssetType.objects.create(name="Car", company=self.company)
        self.asset = Asset.objects.create(
            name="Test Asset",
            asset_type=self.asset_type,
            company=self.company
        )

    def test_formfield_for_dbfield_existing_asset(self):
        inline = AssetIdentifierInline(Asset, self.site)
        path = reverse("admin:assets_asset_change", args=[self.asset.pk])
        request = self.factory.get(path)
        request.user = self.user
        
        db_field = AssetIdentifier._meta.get_field("label")
        formfield = inline.formfield_for_dbfield(db_field, request)
        
        choices = formfield.widget.choices
        self.assertIn(("VIN", "VIN"), choices)
        self.assertIn(("Plate", "Plate"), choices)
        self.assertEqual(len(choices), 3) # "---------" + 2 labels

    def test_formfield_for_dbfield_new_asset_is_free_text(self):
        """The identifiers inline only renders on the change form, but the
        label field must degrade to free text on any add-path request."""
        inline = AssetIdentifierInline(Asset, self.site)
        path = reverse("admin:assets_asset_add")
        request = self.factory.get(path)
        request.user = self.user

        db_field = AssetIdentifier._meta.get_field("label")
        formfield = inline.formfield_for_dbfield(db_field, request)

        self.assertFalse(hasattr(formfield.widget, "choices"))

    def test_formfield_for_dbfield_type_without_labels_is_free_text(self):
        """A type with no labels (and no company fallback) gets a free-text
        label field on the change form."""
        self.company.asset_identifier_labels = []
        self.company.save(update_fields=["asset_identifier_labels"])
        inline = AssetIdentifierInline(Asset, self.site)
        path = reverse("admin:assets_asset_change", args=[self.asset.pk])
        request = self.factory.get(path)
        request.user = self.user

        db_field = AssetIdentifier._meta.get_field("label")
        formfield = inline.formfield_for_dbfield(db_field, request)

        self.assertFalse(hasattr(formfield.widget, "choices"))

    def test_formfield_for_dbfield_type_specific_labels(self):
        """Type labels take precedence over company-wide ones on change."""
        self.asset_type.identifier_labels = ["Matrícula", "Bastidor"]
        self.asset_type.save()
        inline = AssetIdentifierInline(Asset, self.site)
        path = reverse("admin:assets_asset_change", args=[self.asset.pk])
        request = self.factory.get(path)
        request.user = self.user

        db_field = AssetIdentifier._meta.get_field("label")
        formfield = inline.formfield_for_dbfield(db_field, request)

        choices = list(formfield.widget.choices)
        self.assertIn(("Matrícula", "Matrícula"), choices)
        self.assertNotIn(("VIN", "VIN"), choices)
