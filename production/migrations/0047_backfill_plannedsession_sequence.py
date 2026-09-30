from django.db import migrations
from django.db.models import F, OuterRef, Subquery, Window
from django.db.models.functions import RowNumber

CHUNK = 2000


def backfill_company(apps, schema_editor):
    """Scope sessions directly instead of hopping through primary_resource.

    primary_resource is nullable, so sessions without one were invisible to
    every company filter. Subqueries rather than joined F() expressions, which
    UPDATE does not accept.
    """
    Session = apps.get_model("production", "PlannedProductionSession")
    Resource = apps.get_model("production", "ProductionResource")
    Ticket = apps.get_model("production", "ProductionTicket")

    Session.objects.filter(
        company__isnull=True, primary_resource__isnull=False
    ).update(
        company_id=Subquery(
            Resource.objects.filter(pk=OuterRef("primary_resource_id")).values("company_id")[:1]
        )
    )

    Session.objects.filter(
        company__isnull=True, production_ticket__isnull=False
    ).update(
        company_id=Subquery(
            Ticket.objects.filter(pk=OuterRef("production_ticket_id")).values("company_id")[:1]
        )
    )


def backfill_sequence(apps, schema_editor):
    """Freeze each ticket's current stage ordering into `sequence`."""
    Session = apps.get_model("production", "PlannedProductionSession")

    numbered = (
        Session.objects.filter(production_ticket__isnull=False)
        .annotate(
            position=Window(
                expression=RowNumber(),
                partition_by=[F("production_ticket_id")],
                order_by=[F("planned_start").asc(), F("id").asc()],
            )
        )
        .values_list("id", "position")
    )

    # RowNumber is 1-based; sequence is a 0-based ordinal.
    rows = [Session(id=pk, sequence=position - 1) for pk, position in numbered]
    for start in range(0, len(rows), CHUNK):
        Session.objects.bulk_update(rows[start:start + CHUNK], ["sequence"])


class Migration(migrations.Migration):

    dependencies = [
        ("production", "0046_plannedsession_sequence"),
    ]

    operations = [
        migrations.RunPython(backfill_company, migrations.RunPython.noop),
        migrations.RunPython(backfill_sequence, migrations.RunPython.noop),
    ]
