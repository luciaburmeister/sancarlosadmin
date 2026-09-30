import unicodedata
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from production.models import (
    PlannedProductionSession,
    PlannedSessionResource,
    ProductionResource,
)
from production.services.session_resources import machine_content_type_id

CHUNK = 2000


class Command(BaseCommand):
    help = (
        "Collapse legacy one-session-per-resource rows into one session owning "
        "several resources. Mirrors migration 0047 so it can be rehearsed with "
        "--dry-run against a production dump before migrating."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        groups = defaultdict(list)
        rows = PlannedProductionSession.objects.order_by("id").values(
            "id",
            "company_id",
            "production_ticket_id",
            "production_stage",
            "planned_start",
            "planned_end",
            "primary_resource_id",
        )
        for row in rows.iterator(chunk_size=CHUNK):
            key = (
                row["company_id"],
                row["production_ticket_id"],
                unicodedata.normalize("NFC", row["production_stage"] or ""),
                row["planned_start"],
                row["planned_end"],
            )
            groups[key].append(row)

        duplicated = {key: group for key, group in groups.items() if len(group) > 1}
        redundant = sum(len(group) - 1 for group in duplicated.values())

        existing_links = defaultdict(list)
        for session_id, resource_id in PlannedSessionResource.objects.order_by(
            "id"
        ).values_list("planned_session_id", "resource_id").iterator(chunk_size=CHUNK):
            existing_links[session_id].append(resource_id)

        missing_anchors = sum(
            row["primary_resource_id"] not in existing_links.get(row["id"], ())
            for group in groups.values()
            for row in group
            if row["primary_resource_id"] is not None
        )

        self.stdout.write(f"Sessions scanned:        {sum(len(g) for g in groups.values())}")
        self.stdout.write(f"Groups with duplicates:  {len(duplicated)}")
        self.stdout.write(f"Rows to be removed:      {redundant}")
        self.stdout.write(f"Anchors missing a link:  {missing_anchors}")

        for key, group in sorted(duplicated.items(), key=lambda kv: -len(kv[1]))[:10]:
            _company_id, ticket_id, stage, start, _end = key
            resource_ids = [r["primary_resource_id"] for r in group]
            self.stdout.write(
                f"  OP-{ticket_id} · {stage} · {start:%Y-%m-%d %H:%M} "
                f"-> {len(group)} rows, resources {resource_ids}"
            )
        if len(duplicated) > 10:
            self.stdout.write(f"  ... and {len(duplicated) - 10} more groups")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run: nothing written."))
            return

        machine_ct_id = machine_content_type_id()
        resource_content_type = dict(
            ProductionResource.objects.values_list("id", "content_type_id")
        )
        with transaction.atomic():
            new_through = []
            anchor_updates = []
            losers = []
            for group in groups.values():
                survivor = group[0]
                resource_ids = []
                for row in group:
                    if row["primary_resource_id"] is not None:
                        resource_ids.append(row["primary_resource_id"])
                    resource_ids.extend(existing_links.get(row["id"], ()))
                resource_ids = list(dict.fromkeys(resource_ids))

                survivor_links = set(existing_links.get(survivor["id"], ()))
                new_through.extend(
                    PlannedSessionResource(
                        planned_session_id=survivor["id"], resource_id=resource_id
                    )
                    for resource_id in resource_ids
                    if resource_id not in survivor_links
                )

                anchor = next(
                    (
                        resource_id
                        for resource_id in resource_ids
                        if resource_content_type.get(resource_id) == machine_ct_id
                    ),
                    resource_ids[0] if resource_ids else None,
                )
                if anchor != survivor["primary_resource_id"]:
                    anchor_updates.append(
                        PlannedProductionSession(
                            id=survivor["id"], primary_resource_id=anchor
                        )
                    )

                losers.extend(row["id"] for row in group[1:])

            if new_through:
                PlannedSessionResource.objects.bulk_create(
                    new_through,
                    ignore_conflicts=True,
                    batch_size=CHUNK,
                )
            if anchor_updates:
                PlannedProductionSession.objects.bulk_update(
                    anchor_updates, ["primary_resource"], batch_size=CHUNK
                )
            for start in range(0, len(losers), CHUNK):
                PlannedProductionSession.objects.filter(
                    id__in=losers[start:start + CHUNK]
                ).delete()

        self.stdout.write(
            self.style.SUCCESS(f"Merged away {len(losers)} redundant sessions.")
        )
