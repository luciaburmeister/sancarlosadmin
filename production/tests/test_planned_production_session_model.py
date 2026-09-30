from datetime import date, datetime, timezone as dt_timezone

from django.test import TestCase

from company.models import Company, Employee
from production.exceptions import PlannedSessionConflictError
from production.models import PlannedProductionSession, PlannedSessionResource
from production.services.session_scheduler import get_or_create_resource_for_target


class PlannedProductionSessionConflictTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co")
        cls.employee = Employee.objects.create(
            company=cls.company, first_name="Jose", last_name="Murillo",
            hired_at=date(2020, 1, 1), can_work_on_production=True,
        )
        cls.resource = get_or_create_resource_for_target(cls.employee)
        cls.existing = PlannedProductionSession.objects.create(
            production_stage="corte",
            planned_start=datetime(2026, 9, 14, 9, tzinfo=dt_timezone.utc),
            planned_end=datetime(2026, 9, 14, 10, tzinfo=dt_timezone.utc),
            primary_resource=cls.resource,
        )

    def test_conflicts_for_returns_overlapping_session(self):
        qs = PlannedProductionSession.conflicts_for(
            primary_resource_id=self.resource.id,
            start=datetime(2026, 9, 14, 9, 30, tzinfo=dt_timezone.utc),
            end=datetime(2026, 9, 14, 10, 30, tzinfo=dt_timezone.utc),
        )
        self.assertEqual(list(qs), [self.existing])

    def test_conflicts_for_excludes_given_pk(self):
        qs = PlannedProductionSession.conflicts_for(
            primary_resource_id=self.resource.id,
            start=datetime(2026, 9, 14, 9, 30, tzinfo=dt_timezone.utc),
            end=datetime(2026, 9, 14, 10, 30, tzinfo=dt_timezone.utc),
            exclude_pk=self.existing.pk,
        )
        self.assertEqual(list(qs), [])

    def test_conflicts_for_finds_resource_link_when_it_is_not_the_anchor(self):
        linked = PlannedProductionSession.objects.create(
            production_stage="pintura",
            planned_start=datetime(2026, 9, 14, 13, tzinfo=dt_timezone.utc),
            planned_end=datetime(2026, 9, 14, 14, tzinfo=dt_timezone.utc),
        )
        PlannedSessionResource.objects.create(
            planned_session=linked,
            resource=self.resource,
        )

        qs = PlannedProductionSession.conflicts_for(
            primary_resource_id=self.resource.id,
            start=datetime(2026, 9, 14, 13, 30, tzinfo=dt_timezone.utc),
            end=datetime(2026, 9, 14, 14, 30, tzinfo=dt_timezone.utc),
        )

        self.assertEqual(list(qs), [linked])

    def test_assert_no_conflict_raises_on_overlap(self):
        with self.assertRaises(PlannedSessionConflictError) as ctx:
            PlannedProductionSession.assert_no_conflict(
                primary_resource_id=self.resource.id,
                start=datetime(2026, 9, 14, 9, 30, tzinfo=dt_timezone.utc),
                end=datetime(2026, 9, 14, 10, 30, tzinfo=dt_timezone.utc),
            )
        self.assertEqual(ctx.exception.conflicting_session_id, self.existing.id)

    def test_assert_no_conflict_passes_when_free(self):
        PlannedProductionSession.assert_no_conflict(
            primary_resource_id=self.resource.id,
            start=datetime(2026, 9, 14, 11, tzinfo=dt_timezone.utc),
            end=datetime(2026, 9, 14, 12, tzinfo=dt_timezone.utc),
        )
