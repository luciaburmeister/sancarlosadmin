import imaplib
import email
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
import hashlib
import os
from django.core.files.base import ContentFile
from django.db import IntegrityError
from django.utils import timezone
from ..models import IncomingInvoice

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

    def fetch_new_invoices(self, days=90):
        """Fetches emails from the last X days and processes their attachments.
        
        Deduplication is handled by the file_hash unique constraint on
        IncomingInvoice, so we fetch all emails (seen or unseen) and
        skip attachments that have already been imported.
        """
        search_criteria = []
            
        if days:
            date_since = (timezone.now() - timezone.timedelta(days=days)).strftime("%d-%b-%Y")
            search_criteria.append(f'SINCE {date_since}')
            
        search_query = " ".join(search_criteria) if search_criteria else "ALL"
        
        # Search for emails matches the criteria
        status, response = self.mail.search(None, search_query)
        if status != 'OK':
            return 0

        email_ids = response[0].split()
        processed_count = 0

        # Pre-load known hashes for this company within the same date
        # window for O(1) duplicate checks instead of hitting the DB per attachment.
        hash_qs = IncomingInvoice.objects.filter(company=self.config.company)
        if days:
            date_since = timezone.now() - timezone.timedelta(days=days)
            hash_qs = hash_qs.filter(email_date__gte=date_since)
        existing_hashes = set(hash_qs.values_list('file_hash', flat=True))

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

                    if file_hash not in existing_hashes:
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
                                pass
                        
                        try:
                            invoice = IncomingInvoice(
                                company=self.config.company,
                                email_config=self.config,
                                file_hash=file_hash,
                                source_email_id=self.decode_mime_header(msg.get('Message-ID', str(e_id))),
                                sender_email=sender_email,
                                email_date=email_date,
                                original_filename=filename[:500],
                            )
                            invoice.file.save(filename, ContentFile(content), save=True)
                            processed_count += 1
                        except IntegrityError:
                            # Hash already exists in DB (cross-company or outside date window) — skip
                            pass
                        existing_hashes.add(file_hash)

        
        self.config.last_checked = timezone.now()
        self.config.save()
        return processed_count

    def is_invoice(self, filename, subject=""):
        """Check if a file is a potential invoice based on file extension.
        
        We accept all PDF attachments rather than filtering by keywords,
        because many providers don't include 'factura'/'invoice' in
        their filenames or subjects, which caused missed imports.
        """
        return filename.lower().endswith('.pdf')

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

