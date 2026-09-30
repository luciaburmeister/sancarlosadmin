"""The one place that assigns resources to a planned session and maintains
its ``primary_resource`` anchor invariant. Kept small and dependency-free so
the schedulers, optimizer and ``ProductionPlanningService`` can all import it."""

from __future__ import annotations

from django.contrib.contenttypes.models import ContentType

from company.models import Machine
from production.models import PlannedSessionResource, ProductionResource


def machine_content_type_id() -> int:
    """Cached ContentType id for Machine (ContentType.objects is memoised)."""
    return ContentType.objects.get_for_model(Machine).id


def resolve_primary_resource_id(resource_ids: list[int]) -> int | None:
    """The anchor for a session: first machine if any, else first employee,
    respecting the order of *resource_ids*."""
    if not resource_ids:
        return None

    machine_ct = machine_content_type_id()
    kinds = dict(
        ProductionResource.objects.filter(pk__in=resource_ids).values_list(
            "pk", "content_type_id"
        )
    )

    for resource_id in resource_ids:
        if kinds.get(resource_id) == machine_ct:
            return resource_id

    # No machine among them: fall back to the first resource that still exists.
    for resource_id in resource_ids:
        if resource_id in kinds:
            return resource_id

    return None


def sync_session_resources(session, resource_ids) -> list[int]:
    """Make *session* own exactly *resource_ids*, and fix its anchor. Idempotent."""
    wanted = list(dict.fromkeys(int(r) for r in resource_ids))  # de-dupes, keeps order

    existing = set(
        PlannedSessionResource.objects.filter(planned_session=session).values_list(
            "resource_id", flat=True
        )
    )

    to_add = [r for r in wanted if r not in existing]
    to_remove = existing - set(wanted)

    if to_add:
        PlannedSessionResource.objects.bulk_create(
            [
                PlannedSessionResource(planned_session=session, resource_id=resource_id)
                for resource_id in to_add
            ],
            ignore_conflicts=True,
        )

    if to_remove:
        PlannedSessionResource.objects.filter(
            planned_session=session, resource_id__in=to_remove
        ).delete()

    primary_id = resolve_primary_resource_id(wanted)
    if session.primary_resource_id != primary_id:
        session.primary_resource_id = primary_id
        session.save(update_fields=["primary_resource", "updated_at"])

    return wanted
