"""
assets/tests/test_estimated_duration_migration.py

Verifies the estimated_duration backfill migration (0052_estimated_duration)
against data shaped exactly like what's already live in production on main
today: AssetPlanning/AssetPlanningRequest rows with scheduled_time/
scheduled_end_time (and needed_time/end_time) already set, from BEFORE this
branch's migrations ever ran.

This doesn't just read the migration code — it actually replays it: builds
the database at the pre-migration schema (0051), inserts rows through that
old schema, migrates forward to the current head, then asserts what a real
production row would look like afterward. See assets/migrations/0052_estimated_duration.py
and 0055_assetplanningrequest_estimated_duration_required.py.

Run just this file:
    python manage.py test assets.tests.test_estimated_duration_migration
"""
from datetime import date, time, timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

MIGRATE_FROM = [("assets", "0051_assetplanning_skip_auto_assignment_and_more")]
MIGRATE_TO = [("assets", "0055_assetplanningrequest_estimated_duration_required")]


class EstimatedDurationBackfillMigrationTests(TransactionTestCase):
    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps

        OldZone = old_apps.get_model("company", "WorkingZone")
        OldPlanning = old_apps.get_model("assets", "AssetPlanning")
        OldRequest = old_apps.get_model("assets", "AssetPlanningRequest")
        zone = OldZone.objects.create(name="Zone")

        # A normal already-planned row, exactly like what's live on main today.
        self.normal_id = OldPlanning.objects.create(
            origin=zone, destination=zone,
            scheduled_date=date(2026, 1, 5),
            scheduled_time=time(9, 0),
            scheduled_end_time=time(11, 30),
        ).pk

        # Only ever had a start time — nothing to backfill from, should stay blank.
        self.start_only_id = OldPlanning.objects.create(
            origin=zone, destination=zone,
            scheduled_date=date(2026, 1, 6),
            scheduled_time=time(8, 0),
        ).pk

        # Bad legacy data: end before start — must not silently invert it into
        # a duration; leaving it blank is the safe call.
        self.bad_data_id = OldPlanning.objects.create(
            origin=zone, destination=zone,
            scheduled_date=date(2026, 1, 7),
            scheduled_time=time(14, 0),
            scheduled_end_time=time(13, 0),
        ).pk

        self.request_id = OldRequest.objects.create(
            to_zone=zone,
            needed_date=date(2026, 1, 5),
            needed_time=time(7, 30),
            end_time=time(9, 0),
        ).pk

        # A request that never had an end_time at all.
        self.request_no_end_id = OldRequest.objects.create(
            to_zone=zone,
            needed_date=date(2026, 1, 8),
        ).pk

        MigrationExecutor(connection).migrate(MIGRATE_TO)
        # Finish bringing the schema fully current — later migrations don't touch
        # scheduled_time/estimated_duration, but the test bodies below query through
        # the real `assets.models.AssetPlanning`, which expects every column any
        # migration ever added (e.g. `planning`, `abandoned_stage`), not just the
        # ones added by MIGRATE_TO.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def tearDown(self):
        # Leave the DB on the real latest migration state for other tests.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_scheduled_time_survives_untouched(self):
        from assets.models import AssetPlanning
        planning = AssetPlanning.objects.get(pk=self.normal_id)
        self.assertEqual(planning.scheduled_time, time(9, 0))

    def test_estimated_duration_is_computed_from_the_old_end_time(self):
        from assets.models import AssetPlanning
        planning = AssetPlanning.objects.get(pk=self.normal_id)
        self.assertEqual(planning.estimated_duration, timedelta(hours=2, minutes=30))
        self.assertEqual(planning.scheduled_end_time, time(11, 30))  # round-trips via the property

    def test_start_only_row_keeps_a_blank_duration(self):
        from assets.models import AssetPlanning
        planning = AssetPlanning.objects.get(pk=self.start_only_id)
        self.assertEqual(planning.scheduled_time, time(8, 0))
        self.assertIsNone(planning.estimated_duration)

    def test_bad_legacy_data_is_left_blank_not_guessed(self):
        from assets.models import AssetPlanning
        planning = AssetPlanning.objects.get(pk=self.bad_data_id)
        self.assertEqual(planning.scheduled_time, time(14, 0))
        self.assertIsNone(planning.estimated_duration)

    def test_needed_time_survives_and_duration_is_computed_for_requests(self):
        from assets.models import AssetPlanningRequest
        req = AssetPlanningRequest.objects.get(pk=self.request_id)
        self.assertEqual(req.needed_time, time(7, 30))
        self.assertEqual(req.estimated_duration, timedelta(hours=1, minutes=30))

    def test_request_without_an_old_end_time_gets_the_one_hour_default(self):
        # 0055 makes estimated_duration required and defaults any still-blank
        # ones to 1h — this request never had an end_time to backfill from.
        from assets.models import AssetPlanningRequest
        req = AssetPlanningRequest.objects.get(pk=self.request_no_end_id)
        self.assertEqual(req.estimated_duration, timedelta(hours=1))
