from django.core.management.base import BaseCommand, CommandError

from company.models import Company
from inventory.services.item_schema_presets import seed_item_schemas


class Command(BaseCommand):
    help = (
        "Seed a company with the ready-made item metadata schemas: the Shopify "
        "block, the wine block and the 'Vino' product schema using both. Safe to "
        "re-run: only missing schemas and fields are created, edits are kept."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--company", type=int, action="append", required=True,
            help="Company id to seed; repeat for several.",
        )

    def handle(self, *args, **options):
        ids = options["company"]
        companies = list(Company.objects.filter(pk__in=ids))
        unknown = set(ids) - {c.pk for c in companies}
        if unknown:
            raise CommandError(f"Unknown company id(s): {', '.join(map(str, sorted(unknown)))}")

        for company in companies:
            result = seed_item_schemas(company)
            self.stdout.write(self.style.SUCCESS(
                f"{company.name}: {len(result.created_schemas)} schema(s) and "
                f"{len(result.created_fields)} field(s) created."
            ))
            for name in result.created_schemas:
                self.stdout.write(f"  + schema {name}")
