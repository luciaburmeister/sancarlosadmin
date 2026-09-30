"""Coverage for migration 0048 and its rehearsal command.

0048 changes no schema, only data, so its ``apps.get_model`` functions can be
driven with the live app registry. That tests the real migration code without
MigrationExecutor machinery.
"""

from __future__ import annotations

import importlib
import unicodedata
from datetime import datetime, timedelta
from io import StringIO

from django.apps import apps as global_apps
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from production.models import (
    PlannedProductionSession,
    PlannedSessionResource,
    PlannedProductionSessionSource,
)
from production.tests.factories import (
    STAGE_CUT,
    make_company,
    make_employee,
    make_machine,
    make_ticket,
    resource_for,
)

migration = importlib.import_module(
    "production.migrations.0048_merge_per_resource_sessions"
)


class MergePerResourceSessionsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.ticket = make_ticket(cls.company)
        cls.tz = timezone.get_current_timezone()

        cls.operator = resource_for(make_employee(cls.company, first_name="Ana"))
        cls.helper = resource_for(
            make_employee(cls.company, first_name="Luis", last_name="Vega")
        )
        cls.machine = resource_for(make_machine(cls.company, name="CNC-01"))

        cls.start = datetime(2026, 9, 14, 8, 0, tzinfo=cls.tz)
        cls.end = cls.start + timedelta(hours=2)

    def _legacy_session(
        self, resource, *, stage=STAGE_CUT, start=None, end=None, ticket=None
    ):
        """A pre-0048 row: one session per resource, anchor only, no join row."""
        return PlannedProductionSession.objects.create(
            production_ticket=ticket,
            company=self.company,
            production_stage=stage,
            planned_start=start or self.start,
            planned_end=end or self.end,
            primary_resource=resource,
            source=PlannedProductionSessionSource.AUTOMATIC,
        )

    def merge(self):
        migration.merge_sessions(global_apps, None)

    # --- merging ---------------------------------------------------------

    def test_three_rows_collapse_into_one_session_with_three_resources(self):
        for resource in (self.operator, self.helper, self.machine):
            self._legacy_session(resource)

        self.merge()

        self.assertEqual(PlannedProductionSession.objects.count(), 1)
        session = PlannedProductionSession.objects.get()
        self.assertEqual(session.resources.count(), 3)
        self.assertEqual(
            set(session.resources.values_list("resource_id", flat=True)),
            {self.operator.pk, self.helper.pk, self.machine.pk},
        )

    def test_anchor_prefers_the_machine(self):
        self._legacy_session(self.operator)  # lower id, would win by order alone
        self._legacy_session(self.machine)

        self.merge()

        session = PlannedProductionSession.objects.get()
        self.assertEqual(session.primary_resource_id, self.machine.pk)

    def test_anchor_falls_back_to_the_first_employee_when_no_machine(self):
        self._legacy_session(self.operator)
        self._legacy_session(self.helper)

        self.merge()

        session = PlannedProductionSession.objects.get()
        self.assertEqual(session.primary_resource_id, self.operator.pk)

    def test_surviving_session_is_the_lowest_id(self):
        first = self._legacy_session(self.operator)
        self._legacy_session(self.machine)

        self.merge()

        self.assertEqual(PlannedProductionSession.objects.get().pk, first.pk)

    def test_singleton_session_gains_its_join_row(self):
        session = self._legacy_session(self.operator)

        self.merge()

        self.assertEqual(PlannedProductionSession.objects.count(), 1)
        self.assertEqual(
            list(session.resources.values_list("resource_id", flat=True)),
            [self.operator.pk],
        )

    def test_same_stage_at_a_different_interval_stays_separate(self):
        """A stage legitimately split into two passes must not be fused."""
        self._legacy_session(self.operator)
        later = self.start + timedelta(days=1)
        self._legacy_session(self.operator, start=later, end=later + timedelta(hours=2))

        self.merge()

        self.assertEqual(PlannedProductionSession.objects.count(), 2)

    def test_stage_keys_are_compared_nfc_normalized(self):
        composed = unicodedata.normalize("NFC", "mecanización")
        decomposed = unicodedata.normalize("NFD", "mecanización")
        self.assertNotEqual(composed, decomposed)

        self._legacy_session(self.operator, stage=composed)
        self._legacy_session(self.machine, stage=decomposed)

        self.merge()

        self.assertEqual(PlannedProductionSession.objects.count(), 1)
        self.assertEqual(PlannedProductionSession.objects.get().resources.count(), 2)

    def test_existing_join_rows_are_left_alone(self):
        session = self._legacy_session(self.operator)
        PlannedSessionResource.objects.create(
            planned_session=session, resource=self.operator
        )

        self.merge()  # must not raise on the duplicate

        self.assertEqual(session.resources.count(), 1)

    def test_merge_preserves_resources_attached_to_a_deleted_session(self):
        self._legacy_session(self.operator)
        duplicate = self._legacy_session(self.machine)
        PlannedSessionResource.objects.create(
            planned_session=duplicate, resource=self.machine
        )
        PlannedSessionResource.objects.create(
            planned_session=duplicate, resource=self.helper
        )

        self.merge()

        self.assertEqual(PlannedProductionSession.objects.count(), 1)
        self.assertEqual(
            set(PlannedSessionResource.objects.values_list("resource_id", flat=True)),
            {self.operator.pk, self.machine.pk, self.helper.pk},
        )

    def test_merge_is_idempotent(self):
        self._legacy_session(self.operator)
        self._legacy_session(self.machine)

        self.merge()
        self.merge()

        self.assertEqual(PlannedProductionSession.objects.count(), 1)
        self.assertEqual(PlannedSessionResource.objects.count(), 2)

    def test_unplanned_rows_from_different_companies_are_not_merged(self):
        other_company = make_company(name="Other Co")
        other_resource = resource_for(make_employee(other_company, first_name="Eva"))
        self._legacy_session(self.operator)
        PlannedProductionSession.objects.create(
            company=other_company,
            production_stage=STAGE_CUT,
            planned_start=self.start,
            planned_end=self.end,
            primary_resource=other_resource,
        )

        self.merge()

        self.assertEqual(PlannedProductionSession.objects.count(), 2)

    # --- reverse ---------------------------------------------------------

    def test_reverse_re_explodes_one_session_per_resource(self):
        for resource in (self.operator, self.machine, self.helper):
            self._legacy_session(resource)
        self.merge()

        migration.explode_sessions(global_apps, None)

        self.assertEqual(PlannedProductionSession.objects.count(), 3)
        self.assertEqual(PlannedSessionResource.objects.count(), 0)
        self.assertEqual(
            set(
                PlannedProductionSession.objects.values_list(
                    "primary_resource_id", flat=True
                )
            ),
            {self.operator.pk, self.machine.pk, self.helper.pk},
        )

    def test_reverse_preserves_the_session_payload(self):
        self._legacy_session(self.operator)
        self._legacy_session(self.machine)
        self.merge()
        PlannedProductionSession.objects.update(sequence=4, notes="importante")

        migration.explode_sessions(global_apps, None)

        for session in PlannedProductionSession.objects.all():
            self.assertIsNone(session.production_ticket_id)
            self.assertEqual(session.company_id, self.company.pk)
            self.assertEqual(session.production_stage, STAGE_CUT)
            self.assertEqual(session.sequence, 4)
            self.assertEqual(session.notes, "importante")
            self.assertEqual(session.planned_start, self.start)
            self.assertEqual(session.planned_end, self.end)


class MergePlannedSessionsCommandTest(TestCase):
    """The command mirrors the migration so ops can rehearse on a dump."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        tz = timezone.get_current_timezone()
        start = datetime(2026, 9, 14, 8, 0, tzinfo=tz)

        cls.operator = resource_for(make_employee(cls.company))
        cls.machine = resource_for(make_machine(cls.company))

        for resource in (cls.operator, cls.machine):
            PlannedProductionSession.objects.create(
                company=cls.company,
                production_stage=STAGE_CUT,
                planned_start=start,
                planned_end=start + timedelta(hours=2),
                primary_resource=resource,
            )

    def test_dry_run_writes_nothing(self):
        call_command("merge_planned_sessions", "--dry-run", stdout=StringIO())

        self.assertEqual(PlannedProductionSession.objects.count(), 2)
        self.assertEqual(PlannedSessionResource.objects.count(), 0)

    def test_command_merges_like_the_migration(self):
        call_command("merge_planned_sessions", stdout=StringIO())

        self.assertEqual(PlannedProductionSession.objects.count(), 1)
        session = PlannedProductionSession.objects.get()
        self.assertEqual(session.resources.count(), 2)
        self.assertEqual(session.primary_resource_id, self.machine.pk)

    def test_command_keeps_existing_links_on_a_deleted_session(self):
        duplicate = PlannedProductionSession.objects.order_by("id").last()
        helper = resource_for(make_employee(self.company, first_name="Luis"))
        PlannedSessionResource.objects.create(
            planned_session=duplicate, resource=helper
        )

        call_command("merge_planned_sessions", stdout=StringIO())

        self.assertEqual(PlannedProductionSession.objects.count(), 1)
        self.assertEqual(
            set(PlannedSessionResource.objects.values_list("resource_id", flat=True)),
            {self.operator.pk, self.machine.pk, helper.pk},
        )

    def test_command_query_count_does_not_grow_per_session(self):
        with CaptureQueriesContext(connection) as small:
            call_command("merge_planned_sessions", stdout=StringIO())

        start = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.get_current_timezone())
        for offset in range(8):
            PlannedProductionSession.objects.create(
                company=self.company,
                production_stage=STAGE_CUT,
                planned_start=start + timedelta(hours=offset),
                planned_end=start + timedelta(hours=offset + 1),
                primary_resource=self.operator,
            )

        with CaptureQueriesContext(connection) as large:
            call_command("merge_planned_sessions", stdout=StringIO())

        self.assertLessEqual(len(large), len(small) + 2)


class AnchorInvariantTest(TestCase):
    """Cheap guard: every anchor must also be one of the session's resources.

    Catches any future write path that forgets sync_session_resources.
    """

    def test_every_anchor_is_among_its_own_resources(self):
        company = make_company()
        ticket = make_ticket(company)
        tz = timezone.get_current_timezone()
        start = datetime(2026, 9, 14, 8, 0, tzinfo=tz)

        operator = resource_for(make_employee(company))
        machine = resource_for(make_machine(company))
        session = PlannedProductionSession.objects.create(
            production_ticket=ticket,
            company=company,
            production_stage=STAGE_CUT,
            planned_start=start,
            planned_end=start + timedelta(hours=1),
        )
        from production.services.session_resources import sync_session_resources

        sync_session_resources(session, [operator.pk, machine.pk])

        offenders = [
            s.pk
            for s in PlannedProductionSession.objects.filter(
                primary_resource__isnull=False
            ).prefetch_related("resources")
            if s.primary_resource_id
            not in {r.resource_id for r in s.resources.all()}
        ]
        self.assertEqual(offenders, [])
