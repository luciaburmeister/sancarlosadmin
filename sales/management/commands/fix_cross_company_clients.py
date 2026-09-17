"""Fix clients whose company does not match the company of the documents
(invoices, quotes, delivery orders…) that point at them.

The documents are right; the client record is in the wrong company. A client
nobody in its own company uses is moved to the company that does use it; a
client both companies use is copied, and each company's documents follow their
own copy. When the other company already has a client with the same tax
identifier, its documents go to that one instead of a duplicate.

    python manage.py fix_cross_company_clients                 # report only
    python manage.py fix_cross_company_clients --apply
    python manage.py fix_cross_company_clients --client 173 --client 97 --apply
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from company.models import Company
from sales.services.client_company_repair import (
    COPY,
    MERGE,
    MOVE,
    apply_plan,
    build_plans,
    find_mismatched_client_ids,
)


class Command(BaseCommand):
    help = "Re-home or duplicate clients that are used by a company other than their own."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the changes. Without it the command only reports what it would do.",
        )
        parser.add_argument(
            "--client",
            type=int,
            action="append",
            dest="clients",
            help="Restrict to these client ids (repeatable).",
        )
        parser.add_argument(
            "--company",
            type=int,
            action="append",
            dest="companies",
            help="Restrict to clients currently owned by these company ids (repeatable).",
        )

    def handle(self, *args, **options):
        client_ids = options.get("clients") or find_mismatched_client_ids(
            company_ids=options.get("companies")
        )
        plans = build_plans(client_ids)
        if not plans:
            self.stdout.write(self.style.SUCCESS("No client is used by a foreign company."))
            return

        names = dict(Company.objects.values_list("id", "name"))

        def company_label(company_id):
            return f"{names.get(company_id, '?')} (#{company_id})"

        totals = {MOVE: 0, COPY: 0, MERGE: 0}
        for plan in plans:
            self.stdout.write(
                f"\nClient #{plan.client_id} {plan.label} "
                f"[{plan.tax_identifier or 'no tax id'}] — owned by {company_label(plan.company_id)}"
            )
            for company_id, records in sorted(
                plan.usage.items(), key=lambda item: (-item[1], item[0] or 0)
            ):
                where = "no company" if company_id is None else company_label(company_id)
                self.stdout.write(f"    used by {where}: {records} records")
            for action in plan.actions:
                totals[action.kind] += 1
                self.stdout.write(f"  -> {action}")
            for note in plan.notes:
                self.stdout.write(self.style.WARNING(f"  !  {note}"))

        summary = (
            f"{len(plans)} clients: {totals[MOVE]} moved, "
            f"{totals[COPY]} copied, {totals[MERGE]} merged into an existing client"
        )
        if not options["apply"]:
            self.stdout.write(self.style.WARNING(f"\nDry run — {summary}. Re-run with --apply."))
            return

        with transaction.atomic():
            for plan in plans:
                apply_plan(plan)
        self.stdout.write(self.style.SUCCESS(f"\nApplied — {summary}."))
