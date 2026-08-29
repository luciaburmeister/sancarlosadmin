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

The replay (rolling the whole schema back to 0059 and forward again) costs
~15s, so it runs ONCE per class and every check lives in a single test
method as a subTest — one test method per assertion would multiply that
cost by the number of assertions.

Run just this file:
    python manage.py test assets.tests.test_planning_backfill_migration
"""
from datetime import date

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

MIGRATE_FROM = [("assets", "0059_assetplanningrequestdraft_destination_city_and_more")]
MIGRATE_TO = [("assets", "0061_backfill_assettransaction_planning")]


def migrate_to_head():
    """Bring the schema fully current (the real latest migration state)."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())


class PlanningBackfillMigrationTests(TransactionTestCase):
    def setUp(self):
        # Whatever happens below, leave the DB on the latest migration state
        # for the other tests in this worker. addCleanup (unlike tearDown)
        # still runs when setUp itself blows up halfway through the replay.
        self.addCleanup(migrate_to_head)

        executor = MigrationExecutor(connection)
        executor.migrate(MIGRATE_FROM)
        old_apps = executor.loader.project_state(MIGRATE_FROM).apps

        OldCompany = old_apps.get_model("company", "Company")
        OldPlanning = old_apps.get_model("assets", "AssetPlanning")
        OldTransaction = old_apps.get_model("assets", "AssetTransaction")

        # company.0074 (merged in from main after this test was written) added a
        # NOT NULL ArrayField with no server-side default. The historical
        # `OldCompany` model predates that migration and omits the column
        # entirely, so inserts fail unless the column has a temporary DB-level
        # default to fall back on.
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE company_company "
                "ALTER COLUMN asset_plan_draft_notification_emails SET DEFAULT '{}'"
            )

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
        # Finish bringing the schema fully current — the test body below queries
        # through the real `assets.models.AssetTransaction`/`AssetPlanning`, which
        # expect every column any migration ever added, not just the ones added
        # by MIGRATE_TO.
        migrate_to_head()

    def test_backfill_of_live_shaped_rows(self):
        from assets.models import AssetTransaction

        matching = AssetTransaction.objects.get(pk=self.matching_id)
        cross_company = AssetTransaction.objects.get(pk=self.cross_company_id)
        garbage = AssetTransaction.objects.get(pk=self.garbage_id)
        no_planning = AssetTransaction.objects.get(pk=self.no_planning_id)

        with self.subTest("matching same-company planning is backfilled"):
            self.assertEqual(matching.planning_id, self.planning_id)
            # The soft JSON reference is left exactly as it was.
            self.assertEqual(matching.data["planning_id"], self.planning_id)

        with self.subTest("cross-company reference is not backfilled"):
            self.assertIsNone(cross_company.planning_id)

        with self.subTest("garbage planning_id is skipped without raising"):
            self.assertIsNone(garbage.planning_id)

        with self.subTest("row without planning_id is left untouched"):
            self.assertIsNone(no_planning.planning_id)
