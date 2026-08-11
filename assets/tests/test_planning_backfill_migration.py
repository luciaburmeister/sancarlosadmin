"""
assets/tests/test_planning_backfill_migration.py

Verifies the AssetTransaction.planning backfill migration (0061) against
data shaped like what's already live: AssetTransaction rows created before
the FK existed, carrying only a soft `data["planning_id"]` reference.

Replays the migration: builds the database at the pre-migration schema
(0059, before the `planning` field is even added), inserts rows through
that old schema, migrates forward through 0060 (AddField) and 0061
(backfill), then asserts what a real production row looks like afterward.
See assets/migrations/0060_assettransaction_planning.py and
assets/migrations/0061_backfill_assettransaction_planning.py.

Run just this file:
    python manage.py test assets.tests.test_planning_backfill_migration
"""
from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

MIGRATE_FROM = [("assets", "0059_assetplanningrequestdraft_destination_city_and_more")]
MIGRATE_TO = [("assets", "0061_backfill_assettransaction_planning")]


class PlanningBackfillMigrationTests(TransactionTestCase):
    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps

        OldCompany = old_apps.get_model("company", "Company")
        OldPlanning = old_apps.get_model("assets", "AssetPlanning")
        OldTransaction = old_apps.get_model("assets", "AssetTransaction")

        self.company_a_id = OldCompany.objects.create(name="Company A").pk
        company_b_id = OldCompany.objects.create(name="Company B").pk

        self.planning_id = OldPlanning.objects.create(
            company_id=self.company_a_id, scheduled_date=date(2026, 1, 5),
        ).pk

        # Matching planning_id, same company as the planning — should backfill.
        self.matching_id = OldTransaction.objects.create(
            company_id=self.company_a_id, transaction_type="movement",
            data={"planning_id": self.planning_id},
        ).pk

        # References a real planning, but belongs to a different company —
        # must stay unlinked (never cross tenants).
        self.cross_company_id = OldTransaction.objects.create(
            company_id=company_b_id, transaction_type="movement",
            data={"planning_id": self.planning_id},
        ).pk

        # Garbage planning_id — must be skipped, not raise.
        self.garbage_id = OldTransaction.objects.create(
            company_id=self.company_a_id, transaction_type="movement",
            data={"planning_id": "not-a-number"},
        ).pk

        # No planning_id at all — untouched.
        self.no_planning_id = OldTransaction.objects.create(
            company_id=self.company_a_id, transaction_type="movement",
            data={},
        ).pk

        MigrationExecutor(connection).migrate(MIGRATE_TO)

    def tearDown(self):
        # Leave the DB on the real latest migration state for other tests.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_matching_same_company_planning_is_backfilled(self):
        from assets.models import AssetTransaction
        tx = AssetTransaction.objects.get(pk=self.matching_id)
        self.assertEqual(tx.planning_id, self.planning_id)
        # The soft JSON reference is left exactly as it was.
        self.assertEqual(tx.data["planning_id"], self.planning_id)

    def test_cross_company_reference_is_not_backfilled(self):
        from assets.models import AssetTransaction
        tx = AssetTransaction.objects.get(pk=self.cross_company_id)
        self.assertIsNone(tx.planning_id)

    def test_garbage_planning_id_is_skipped_without_raising(self):
        from assets.models import AssetTransaction
        tx = AssetTransaction.objects.get(pk=self.garbage_id)
        self.assertIsNone(tx.planning_id)

    def test_row_without_planning_id_is_left_untouched(self):
        from assets.models import AssetTransaction
        tx = AssetTransaction.objects.get(pk=self.no_planning_id)
        self.assertIsNone(tx.planning_id)
