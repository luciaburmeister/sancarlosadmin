import json
import os
from django.core.management.base import BaseCommand
from sales.services.cortizo_import import CortizoImportService

class Command(BaseCommand):
    help = "Import Cortizo sales documents from JSON file"

    def add_arguments(self, parser):
        parser.add_argument("json_file", type=str, help="Path to the JSON file")
        parser.add_argument("--company", type=str, default="Aluares", help="Company name")

    def handle(self, *args, **options):
        json_file = options["json_file"]
        company_name = options["company"]

        if not os.path.exists(json_file):
            self.stderr.write(f"File not found: {json_file}")
            return

        with open(json_file, "r") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as e:
                self.stderr.write(f"Invalid JSON: {e}")
                return

        service = CortizoImportService(company_name=company_name)
        quotes = service.import_documents(data)

        for quote in quotes:
            self.stdout.write(self.style.SUCCESS(f"Imported Quote: {quote.id} for {quote.client.name}"))
