from django.test import TestCase
from company.models import EmployeeDocumentType, MachineDocumentType
from assets.models import AssetDocumentType
from company.admin.employee import EmployeeDocumentInline
from company.admin.machine import MachineDocumentInline
from assets.admin.asset import AssetDocumentInline
from unfold.admin import StackedInline

class DocumentExpiryTest(TestCase):
    def test_does_not_expire_field_exists(self):
        self.assertTrue(hasattr(EmployeeDocumentType, "does_not_expire"))
        self.assertTrue(hasattr(MachineDocumentType, "does_not_expire"))
        self.assertTrue(hasattr(AssetDocumentType, "does_not_expire"))

    def test_inlines_use_stacked_inline_and_fieldsets(self):
        self.assertTrue(issubclass(EmployeeDocumentInline, StackedInline))
        self.assertTrue(issubclass(MachineDocumentInline, StackedInline))
        self.assertTrue(issubclass(AssetDocumentInline, StackedInline))
        
        # Check fieldsets
        self.assertIsNotNone(EmployeeDocumentInline.fieldsets)
        self.assertIsNotNone(MachineDocumentInline.fieldsets)
        self.assertIsNotNone(AssetDocumentInline.fieldsets)
        
        # Verify grouped fields (tuples) are used for space optimization
        fields = EmployeeDocumentInline.fieldsets[0][1]["fields"]
        self.assertIsInstance(fields[0], tuple)
        self.assertIsInstance(fields[1], tuple)
