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

    def test_create_employee_document(self):
        file_content = b"fake file content"
        uploaded_file = SimpleUploadedFile("test_doc.pdf", file_content, content_type="application/pdf")
        
        doc = EmployeeDocument.objects.create(
            company=self.company,
            employee=self.employee,
            document_type=self.doc_type,
            file=uploaded_file,
            expiry_date=date(2025, 1, 1)
        )
        
        self.assertEqual(EmployeeDocument.objects.count(), 1)
        self.assertEqual(doc.employee, self.employee)
        self.assertEqual(doc.document_type, self.doc_type)
        self.assertTrue(doc.file.name.endswith(".pdf"))

    def test_document_history(self):
        file_content = b"fake file content"
        uploaded_file = SimpleUploadedFile("test_doc.pdf", file_content, content_type="application/pdf")
        
        doc = EmployeeDocument.objects.create(
            company=self.company,
            employee=self.employee,
            document_type=self.doc_type,
            file=uploaded_file,
            expiry_date=date(2025, 1, 1)
        )
        
        # Change expiry date (simulating renewal)
        doc.expiry_date = date(2026, 1, 1)
        doc.save()
        
        # Verify history
        self.assertEqual(doc.history.count(), 2)
        history_records = doc.history.all().order_by('history_date')
        self.assertEqual(history_records[0].expiry_date, date(2025, 1, 1))
        self.assertEqual(history_records[1].expiry_date, date(2026, 1, 1))

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
