from django.test import TestCase
from django.core.files.uploadedfile import SimpleUploadedFile
from company.models import Company, Employee, EmployeeType, EmployeeDocument, EmployeeDocumentType
from datetime import date

class DocumentTestCase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Test Company")
        self.employee_type = EmployeeType.objects.create(company=self.company, type="Office")
        self.employee = Employee.objects.create(
            company=self.company,
            first_name="John",
            last_name="Doe",
            hired_at=date(2023, 1, 1),
            type=self.employee_type
        )
        self.doc_type = EmployeeDocumentType.objects.create(company=self.company, name="ID Card")

    def test_automated_company_assignment(self):
        file_content = b"fake file content"
        uploaded_file = SimpleUploadedFile("test_doc.pdf", file_content, content_type="application/pdf")
        
        # Create document without explicitly setting company
        doc = EmployeeDocument.objects.create(
            employee=self.employee,
            document_type=self.doc_type,
            file=uploaded_file,
            expiry_date=date(2025, 1, 1)
        )
        
        self.assertEqual(doc.company, self.company)
