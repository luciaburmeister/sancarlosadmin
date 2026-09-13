from django.core.management.base import BaseCommand

from inventory.services.stock_position import find_drift, refresh_all


class Command(BaseCommand):
    help = (
        "Recompute every inventory item's cached incoming and reserved "
        "quantities from source. Run once after deploying the columns to fill "
        "them in, and any time afterwards to repair drift; use --check to "
        "report drift without writing anything."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Report items whose cached figures are stale, then exit "
                 "without changing anything.",
        )

    def handle(self, *args, **options):
        if options["check"]:
            return self._check()

        touched = refresh_all()
        self.stdout.write(self.style.SUCCESS(f"Refreshed {touched} item(s)."))

    def _check(self):
        drifted = list(
            find_drift().values_list(
                "code", "incoming_quantity", "computed_incoming",
                "reserved_quantity", "computed_reserved",
            )
        )
        if not drifted:
            self.stdout.write(self.style.SUCCESS("No drift: every item is in sync."))
            return

        self.stdout.write(
            self.style.WARNING(f"{len(drifted)} item(s) out of sync:")
        )
        for code, incoming, real_incoming, reserved, real_reserved in drifted:
            self.stdout.write(
                f"  {code}: incoming {incoming} → {real_incoming}, "
                f"reserved {reserved} → {real_reserved}"
            )
        self.stdout.write("Run without --check to repair.")
