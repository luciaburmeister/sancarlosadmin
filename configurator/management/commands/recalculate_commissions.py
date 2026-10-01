from django.core.management.base import BaseCommand

from configurator.commission.services import CommissionService
from sales.models import Quote


class Command(BaseCommand):
    help = (
        "Recalculate the commission of every quote with an agent from its "
        "current totals and the agent's current rule (fixes amounts left "
        "stale by edits made before commissions followed every re-total)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--company", type=int, help="Only this company's quotes.")
        parser.add_argument("--dry-run", action="store_true", help="Count the quotes, change nothing.")

    def handle(self, *args, **options):
        quotes = Quote.objects.filter(agent__isnull=False)
        if options["company"]:
            quotes = quotes.filter(company_id=options["company"])

        count = quotes.count()
        if options["dry_run"]:
            self.stdout.write(f"{count} quotes would be recalculated.")
            return

        for quote in quotes.iterator():
            CommissionService.refresh(quote)
        self.stdout.write(self.style.SUCCESS(f"Recalculated commissions on {count} quotes."))
