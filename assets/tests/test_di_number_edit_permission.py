"""
assets/tests/test_di_number_edit_permission.py

di_number is auto-assigned and normally read-only everywhere (see
AssetTransaction.requires_di_number) -- but a superuser can hand-correct a
genuine mistake directly in the admin. Everyone else still can't.
"""
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from unfold.widgets import UnfoldAdminIntegerFieldWidget

from assets.admin.asset_transaction import (
    AssetTransactionAdmin,
    AssetTransactionAdminForm,
    AssetTransactionReceiptAdmin,
    AssetTransactionReceiptAdminForm,
)
from assets.models import AssetTransaction, AssetTransactionReceipt

User = get_user_model()


class DiNumberEditPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(username="super", email="s@s.com", password="x")
        cls.staff = User.objects.create_user(username="staff", password="x", is_staff=True)

    def _request(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return request

    def test_superuser_can_edit_di_number_on_asset_transaction(self):
        admin_instance = AssetTransactionAdmin(AssetTransaction, AdminSite())

        fields = admin_instance.get_readonly_fields(self._request(self.superuser))

        self.assertNotIn("di_number", fields)

    def test_regular_staff_cannot_edit_di_number_on_asset_transaction(self):
        admin_instance = AssetTransactionAdmin(AssetTransaction, AdminSite())

        fields = admin_instance.get_readonly_fields(self._request(self.staff))

        self.assertIn("di_number", fields)

    def test_superuser_can_edit_di_number_on_receipt(self):
        admin_instance = AssetTransactionReceiptAdmin(AssetTransactionReceipt, AdminSite())

        fields = admin_instance.get_readonly_fields(self._request(self.superuser))

        self.assertNotIn("di_number", fields)

    def test_regular_staff_cannot_edit_di_number_on_receipt(self):
        admin_instance = AssetTransactionReceiptAdmin(AssetTransactionReceipt, AdminSite())

        fields = admin_instance.get_readonly_fields(self._request(self.staff))

        self.assertIn("di_number", fields)

    def test_di_number_uses_the_unfold_widget_on_asset_transaction(self):
        form = AssetTransactionAdminForm()

        self.assertIsInstance(form.fields["di_number"].widget, UnfoldAdminIntegerFieldWidget)

    def test_di_number_uses_the_unfold_widget_on_receipt(self):
        form = AssetTransactionReceiptAdminForm()

        self.assertIsInstance(form.fields["di_number"].widget, UnfoldAdminIntegerFieldWidget)

    def test_regular_staff_can_open_the_asset_transaction_form(self):
        """get_form() excludes di_number from the generated form for a
        non-superuser (see get_readonly_fields) -- the custom __init__ must
        not assume it's always present, or opening the change page as
        regular staff crashes with a KeyError."""
        admin_instance = AssetTransactionAdmin(AssetTransaction, AdminSite())
        form_class = admin_instance.get_form(self._request(self.staff))

        form = form_class()

        self.assertNotIn("di_number", form.fields)

    def test_regular_staff_can_open_the_receipt_form(self):
        admin_instance = AssetTransactionReceiptAdmin(AssetTransactionReceipt, AdminSite())
        form_class = admin_instance.get_form(self._request(self.staff))

        form = form_class()

        self.assertNotIn("di_number", form.fields)
