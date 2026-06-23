from django.test import TestCase
from django.utils import timezone
from datetime import timedelta, date
from django.core import mail
from django.conf import settings
from company.models import Company, Employee, EmployeeDocument, EmployeeDocumentType
from company.tasks import send_document_reminders

class DocumentReminderTestCase(TestCase):
    def setUp(self):
        # Ensure BACKEND_URL exists for tests
        if not hasattr(settings, 'BACKEND_URL'):
            settings.BACKEND_URL = "http://testserver"
            
        self.company = Company.objects.create(
            name="Test Company",
            email="company@test.com",
            document_reminder_emails=["remind1@test.com", "remind2@test.com"]
        )
        self.employee = Employee.objects.create(
            company=self.company,
            first_name="John",
            last_name="Doe",
            hired_at=date(2023, 1, 1)
        )
        self.doc_type = EmployeeDocumentType.objects.create(
            company=self.company, 
            name="ID Card",
            notify_before_days=30
        )

    def test_reminder_sent_for_latest_doc(self):
        # Create a document expiring in 10 days
        expiry = timezone.now().date() + timedelta(days=10)
        doc = EmployeeDocument.objects.create(
            employee=self.employee,
            document_type=self.doc_type,
            expiry_date=expiry,
            file="test.pdf"
        )

        result = send_document_reminders()
        
        # Verify email sent
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["remind1@test.com", "remind2@test.com"])
        self.assertIn("Test Company", mail.outbox[0].subject)
        
        # Verify marked as sent
        doc.refresh_from_db()
        self.assertIsNotNone(doc.reminder_sent_at)
        self.assertEqual(result["emails_sent"], 1)

    def test_grouped_reminders(self):
        # Create 2 documents expiring in 10 days for the same company
        expiry = timezone.now().date() + timedelta(days=10)
        EmployeeDocument.objects.create(
            employee=self.employee,
            document_type=self.doc_type,
            expiry_date=expiry,
            file="test1.pdf"
        )
        
        # Another doc type
        doc_type2 = EmployeeDocumentType.objects.create(
            company=self.company, 
            name="Insurance",
            notify_before_days=30
        )
        EmployeeDocument.objects.create(
            employee=self.employee,
            document_type=doc_type2,
            expiry_date=expiry,
            file="test2.pdf"
        )

        result = send_document_reminders()
        
        # Should only send ONE email for the company
        self.assertEqual(len(mail.outbox), 1)
        # The email body should mention both documents
        # Since it's HTML, we'll check common fragments
        self.assertIn("ID Card", mail.outbox[0].alternatives[0][0])
        self.assertIn("Insurance", mail.outbox[0].alternatives[0][0])
        self.assertEqual(result["emails_sent"], 1)

    def test_reminder_suppressed_if_newer_doc_exists_with_later_expiry(self):
        # Doc 1: Expiring in 10 days (old)
        expiry1 = timezone.now().date() + timedelta(days=10)
        doc1 = EmployeeDocument.objects.create(
            employee=self.employee,
            document_type=self.doc_type,
            expiry_date=expiry1,
            file="test1.pdf"
        )

        # Doc 2: Expiring in 1 year (new)
        expiry2 = timezone.now().date() + timedelta(days=365)
        doc2 = EmployeeDocument.objects.create(
            employee=self.employee,
            document_type=self.doc_type,
            expiry_date=expiry2,
            file="test2.pdf"
        )

        result = send_document_reminders()
        
        # No email should be sent for doc1 because doc2 exists
        # No email should be sent for doc2 because it's not yet expiring
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(result["emails_sent"], 0)
