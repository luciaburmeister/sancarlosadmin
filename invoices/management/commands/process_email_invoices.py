from django.core.management.base import BaseCommand
from company.models import CompanyEmailConfig
from invoices.services.email_processor import EmailInvoiceProcessor

class Command(BaseCommand):
    help = "Processes all configured company email accounts to download new invoices."

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=90,
            help='Number of days to look back (default: 90)'
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help='Fetch all emails (ignore UNSEEN flag)'
        )

    def handle(self, *args, **options):
        days = options['days']
        only_unseen = not options['all']
        
        self.stdout.write(f"Starting email invoice processing (lookback: {days} days, only_unseen: {only_unseen})...")
        
        configs = CompanyEmailConfig.objects.all()
        if not configs.exists():
            self.stdout.write(self.style.WARNING("No company email configurations found."))
            return

        for config in configs:
            self.stdout.write(f"Processing: {config.email_address} ({config.company})")
            processor = EmailInvoiceProcessor(config)
            try:
                processor.connect()
                count = processor.fetch_new_invoices(days=days, only_unseen=only_unseen)
                processor.disconnect()
                
                if count > 0:
                    self.stdout.write(self.style.SUCCESS(f"Successfully processed {count} new invoices for {config.email_address}."))
                else:
                    self.stdout.write(f"No new invoices found for {config.email_address}.")
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Error processing {config.email_address}: {str(e)}"))

        self.stdout.write(self.style.SUCCESS("Email invoice processing completed."))
