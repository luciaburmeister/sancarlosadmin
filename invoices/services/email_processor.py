import imaplib
import email
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
import hashlib
import os
from django.core.files.base import ContentFile
from django.utils import timezone
from ..models import IncomingInvoice, CompanyEmailConfig

class EmailInvoiceProcessor:
    def __init__(self, email_config):
        self.config = email_config
        self.mail = None

    def connect(self):
        """Establishes connection to the IMAP server."""
        if self.config.use_ssl:
            self.mail = imaplib.IMAP4_SSL(self.config.imap_server, self.config.imap_port)
        else:
            self.mail = imaplib.IMAP4(self.config.imap_server, self.config.imap_port)
        
        self.mail.login(self.config.imap_user, self.config.imap_password)
        self.mail.select('INBOX')

    def disconnect(self):
        """Logs out from the mail server."""
        if self.mail:
            self.mail.logout()

    def fetch_new_invoices(self):
        """Fetches unread emails and processes their attachments."""
        # Search for all unseen emails
        status, response = self.mail.search(None, 'UNSEEN')
        if status != 'OK':
            return []

        email_ids = response[0].split()
        processed_count = 0

        for e_id in email_ids:
            status, msg_data = self.mail.fetch(e_id, '(BODY.PEEK[])')
            if status != 'OK':
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            
            # Process each part of the email
            for part in msg.walk():
                if part.get_content_maintype() == 'multipart':
                    continue
                if part.get('Content-Disposition') is None:
                    continue

                filename = self.decode_mime_header(part.get_filename())
                subject = self.decode_mime_header(msg.get('Subject', ''))
                if filename and self.is_invoice(filename, subject):
                    content = part.get_payload(decode=True)
                    file_hash = self.calculate_hash(content)

                    if not IncomingInvoice.objects.filter(file_hash=file_hash).exists():
                        # Extract and decode sender email
                        from_header = self.decode_mime_header(msg.get('From', ''))
                        _, sender_email = parseaddr(from_header)

                        # Extract and parse email date
                        date_header = msg.get('Date')
                        email_date = None
                        if date_header:
                            try:
                                email_date = parsedate_to_datetime(date_header)
                            except Exception:
                                passI
                        
                        invoice = IncomingInvoice(
                            company=self.config.company,
                            email_config=self.config,
                            file_hash=file_hash,
                            source_email_id=self.decode_mime_header(msg.get('Message-ID', str(e_id))),
                            sender_email=sender_email,
                            email_date=email_date
                        )
                        # The upload_path uses email_date if available
                        invoice.file.save(filename, ContentFile(content), save=True)
                        processed_count += 1

        
        self.config.last_checked = timezone.now()
        self.config.save()
        return processed_count

    def is_invoice(self, filename, subject=""):
        """Check if a file is an invoice based on filename, extension, and email subject."""
        name_lower = filename.lower()
        subject_lower = str(subject).lower()
        
        # Common invoice keywords
        invoice_keywords = ['invoice', 'factura', 'bill', 'receipt']
        
        is_pdf = name_lower.endswith('.pdf')
        has_keyword_in_filename = any(keyword in name_lower for keyword in invoice_keywords)
        has_keyword_in_subject = any(keyword in subject_lower for keyword in invoice_keywords)
        
        return is_pdf and (has_keyword_in_filename or has_keyword_in_subject)

    def decode_mime_header(self, s):
        """Decodes MIME-encoded headers (e.g. subject, filename)."""
        if not s:
            return ""
        try:
            decoded = decode_header(s)
            result = ""
            for content, charset in decoded:
                if isinstance(content, bytes):
                    if charset:
                        result += content.decode(charset)
                    else:
                        result += content.decode('utf-8', errors='replace')
                else:
                    result += str(content)
            return result
        except Exception:
            return s

    def calculate_hash(self, content):
        """Calculates SHA-256 hash of file content."""
        return hashlib.sha256(content).hexdigest()

