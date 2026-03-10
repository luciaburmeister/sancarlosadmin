import os
import django
import sys

# Setup Django environment
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'alumniniooerp.settings')
django.setup()

from invoices.services.email_processor import EmailInvoiceProcessor
from company.models import Company, CompanyEmailConfig

def test_processor_logic():
    print("Testing EmailInvoiceProcessor logic with MIME decoding...")
    
    config = CompanyEmailConfig(
        email_address="test@example.com",
        imap_server="imap.example.com",
        imap_user="test",
        imap_password="password"
    )
    processor = EmailInvoiceProcessor(config)
    
    # Test decoding logic
    encoded_subject = "=?utf-8?B?TnVlc3RyYSBGYWN0dXJhIDAwMDAwMDYwMzUtVzI2MDE4Mi5QREY=?="
    decoded_subject = processor.decode_mime_header(encoded_subject)
    print(f"Encoded: {encoded_subject}")
    print(f"Decoded: {decoded_subject}")
    assert "Factura" in decoded_subject

    # Test is_invoice logic with decoded values
    test_cases = [
        # (filename, subject, expected)
        ("invoice_123.pdf", "Your purchase", True),
        ("0000006035-W252278.PDF", "Factura de Urbanclean", True),
        ("attachment.pdf", decoded_subject, True), # Using the decoded subject
        ("factura_january.pdf", "Sin asunto", True),
        ("bill_electric.pdf", "Monthly bill", True),
        ("receipt.pdf", "Payment receipt", True),
        ("document.docx", "Invoice content", False),
        ("image.png", "Factura", False),
        ("salary.pdf", "Hello there", False)
    ]
    
    for filename, subject, expected in test_cases:
        result = processor.is_invoice(filename, subject)
        print(f"File: {filename:25} Subject: {subject[:30]:30} Expected: {str(expected):5} Result: {str(result):5}")
        assert result == expected
    
    print("\nLogic tests passed!")

if __name__ == "__main__":
    test_processor_logic()
