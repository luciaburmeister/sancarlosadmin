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

    def test_formfield_for_dbfield_new_asset_single_company(self):
        inline = AssetIdentifierInline(Asset, self.site)
        path = reverse("admin:assets_asset_add")
        request = self.factory.get(path)
        # We need a non-superuser to test the "single company" logic easily 
        # because superusers get all companies.
        regular_user = User.objects.create(username="regular", password="password")
        CompanyUser.objects.create(user=regular_user, company=self.company)
        request.user = regular_user
        
        db_field = AssetIdentifier._meta.get_field("label")
        formfield = inline.formfield_for_dbfield(db_field, request)
        
        choices = formfield.widget.choices
        self.assertIn(("VIN", "VIN"), choices)
        self.assertIn(("Plate", "Plate"), choices)

    def test_formfield_for_dbfield_new_asset_multiple_companies(self):
        inline = AssetIdentifierInline(Asset, self.site)
        path = reverse("admin:assets_asset_add")
        request = self.factory.get(path)
        
        company2 = Company.objects.create(name="Company 2")
        regular_user = User.objects.create(username="multi", password="password")
        CompanyUser.objects.create(user=regular_user, company=self.company)
        CompanyUser.objects.create(user=regular_user, company=company2)
        request.user = regular_user
        
        db_field = AssetIdentifier._meta.get_field("label")
        formfield = inline.formfield_for_dbfield(db_field, request)
        
        # Should not have choices because it's ambiguous which company's labels to use
        # (It will fallback to the default CharField widget or an empty choices widget)
        if hasattr(formfield.widget, "choices"):
            self.assertEqual(len(formfield.widget.choices), 0) # Or just default behavior
