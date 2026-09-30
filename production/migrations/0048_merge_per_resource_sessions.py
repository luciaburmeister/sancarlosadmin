"""Collapse one-session-per-resource into one session with N resources.

`session_scheduler` and the optimizer's `apply` step used to create a separate
PlannedProductionSession for every resource of a stage, so a stage worked by a
machine *and* an operator existed as two unrelated rows. Both writers were fixed
first (see production/services/session_resources.py); this migration folds the
historical rows into the correct shape.

Run `manage.py merge_planned_sessions --dry-run` beforehand to see how many
groups will be affected.
"""

import unicodedata
from collections import defaultdict

from django.db import migrations

CHUNK = 2000


def _group_key(row):
    """Sessions merge only when ticket, stage AND the exact interval match.

    Keying on (ticket, stage) alone would wrongly fuse a stage legitimately
    split into two passes on different days.
    """
    return (
        row["company_id"],
        row["production_ticket_id"],
        unicodedata.normalize("NFC", row["production_stage"] or ""),
        row["planned_start"],
        row["planned_end"],
    )


def merge_sessions(apps, schema_editor):
    Session = apps.get_model("production", "PlannedProductionSession")
    Through = apps.get_model("production", "PlannedSessionResource")
    Resource = apps.get_model("production", "ProductionResource")
    ContentType = apps.get_model("contenttypes", "ContentType")

    machine_ct_id = (
        ContentType.objects.filter(app_label="company", model="machine")
        .values_list("id", flat=True)
        .first()
    )
    resource_content_type = dict(Resource.objects.values_list("id", "content_type_id"))

    # Planning tables hold thousands of rows, not millions; grouping in memory
    # keeps this readable and is a single pass over the table.
    groups = defaultdict(list)
    rows = Session.objects.order_by("id").values(
        "id",
        "company_id",
        "production_ticket_id",
        "production_stage",
        "planned_start",
        "planned_end",
        "primary_resource_id",
    )
    for row in rows.iterator(chunk_size=CHUNK):
        groups[_group_key(row)].append(row)

    existing_links = defaultdict(list)
    for session_id, resource_id in Through.objects.order_by("id").values_list(
        "planned_session_id", "resource_id"
    ).iterator(chunk_size=CHUNK):
        existing_links[session_id].append(resource_id)

    new_through = []
    anchor_updates = []
    losers = []

    for group in groups.values():
        survivor = group[0]  # ordered by id, so this is min(id)

        # Keep both legacy anchors and links already attached to any row.
        resource_ids = []
        for row in group:
            if row["primary_resource_id"] is not None:
                resource_ids.append(row["primary_resource_id"])
            resource_ids.extend(existing_links.get(row["id"], ()))
        resource_ids = list(dict.fromkeys(resource_ids))

        # Also covers singleton groups: every session ends up owning its anchor.
        survivor_links = set(existing_links.get(survivor["id"], ()))
        new_through.extend(
            Through(planned_session_id=survivor["id"], resource_id=resource_id)
            for resource_id in resource_ids
            if resource_id not in survivor_links
        )

        anchor = next(
            (r for r in resource_ids if resource_content_type.get(r) == machine_ct_id),
            None,
        )
        if anchor is None and resource_ids:
            anchor = resource_ids[0]

        if anchor != survivor["primary_resource_id"]:
            anchor_updates.append(Session(id=survivor["id"], primary_resource_id=anchor))

        losers.extend(row["id"] for row in group[1:])

    # The replay in 0050 only writes links that are still missing.
    if new_through:
        Through.objects.bulk_create(new_through, ignore_conflicts=True, batch_size=CHUNK)

    if anchor_updates:
        Session.objects.bulk_update(anchor_updates, ["primary_resource"], batch_size=CHUNK)

    for start in range(0, len(losers), CHUNK):
        Session.objects.filter(id__in=losers[start:start + CHUNK]).delete()


def explode_sessions(apps, schema_editor):
    """Re-create one session per resource and drop the join rows.

    Restores the previous *semantics*, not the previous primary keys.
    """
    Session = apps.get_model("production", "PlannedProductionSession")
    Through = apps.get_model("production", "PlannedSessionResource")

    by_session = defaultdict(list)
    for planned_session_id, resource_id in Through.objects.order_by("id").values_list(
        "planned_session_id", "resource_id"
    ):
        by_session[planned_session_id].append(resource_id)

    copied_fields = (
        "production_ticket_id",
        "company_id",
        "production_stage",
        "sequence",
        "planned_start",
        "planned_end",
        "locked",
        "source",
        "notes",
    )

    clones = []
    anchor_updates = []

    originals = Session.objects.filter(id__in=list(by_session)).values(
        "id", *copied_fields
    )
    for original in originals.iterator(chunk_size=CHUNK):
        resource_ids = by_session[original["id"]]

        anchor_updates.append(
            Session(id=original["id"], primary_resource_id=resource_ids[0])
        )

        base = {field: original[field] for field in copied_fields}
        clones.extend(
            Session(primary_resource_id=resource_id, **base)
            for resource_id in resource_ids[1:]
        )

    if anchor_updates:
        Session.objects.bulk_update(anchor_updates, ["primary_resource"], batch_size=CHUNK)

    Session.objects.bulk_create(clones, batch_size=CHUNK)
    Through.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0047_backfill_plannedsession_sequence"),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [
        migrations.RunPython(merge_sessions, explode_sessions),
    ]
