"""
assets/tests/test_transaction_pause.py

Covers assets.services.transaction_pause.TransactionPauseService -- gives a
full-cuba MOVEMENT a second DI (AssetTransactionPause) when it sat paused
across a day boundary before being resolved, whether already dumped on a
later day or still pending today. The verteo's own DI never changes because
of this -- it always matches the movement's original number (see
AssetTransactionReceipt._resolve_di_number, untouched here).
"""
import datetime

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone
from unfold.widgets import UnfoldAdminSplitDateTimeWidget

from api.fleet.tests.helpers import make_asset, make_asset_type, make_company
from assets.admin.asset_transaction import AssetTransactionPauseAdmin, AssetTransactionPauseAdminForm
from assets.models import (
    AssetTransaction,
    AssetTransactionPause,
    AssetTransactionReceipt,
    TransactionTypeChoices,
)
from assets.services.transaction_pause import TransactionPauseService

User = get_user_model()


def _aware(local_date, hour=10):
    return timezone.make_aware(datetime.datetime.combine(local_date, datetime.time(hour, 0)))


class TransactionPauseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.asset_type = make_asset_type(cls.company)
        cls.asset = make_asset(cls.company, cls.asset_type)
        cls.today = timezone.localdate()
        cls.yesterday = cls.today - datetime.timedelta(days=1)

    def _full_cuba_movement(self, date):
        return AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=_aware(date),
            data={"cubaEstado": "Cuba llena"},
        )

    def _receipt_for(self, movement, date):
        receipt_tx = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=_aware(date, hour=18),
        )
        return AssetTransactionReceipt.objects.create(
            transaction=receipt_tx, nro_albaran="01083485", movement_transaction=movement,
        )

    def test_resolved_same_day_needs_no_pause(self):
        movement = self._full_cuba_movement(self.yesterday)
        self._receipt_for(movement, self.yesterday)

        TransactionPauseService.generate_due_pauses(today=self.today)

        self.assertFalse(AssetTransactionPause.objects.filter(movement=movement).exists())

    def test_resolved_the_next_day_gets_a_pause(self):
        movement = self._full_cuba_movement(self.yesterday)
        self._receipt_for(movement, self.today)

        created = TransactionPauseService.generate_due_pauses(today=self.today)

        self.assertEqual(len(created), 1)
        pause = AssetTransactionPause.objects.get(movement=movement)
        self.assertIsNotNone(pause.di_number)
        self.assertNotEqual(pause.di_number, movement.di_number)

    def test_still_pending_from_a_past_day_gets_a_pause(self):
        movement = self._full_cuba_movement(self.yesterday)  # no receipt at all yet

        created = TransactionPauseService.generate_due_pauses(today=self.today)

        self.assertEqual(len(created), 1)
        self.assertTrue(AssetTransactionPause.objects.filter(movement=movement).exists())

    def test_still_pending_from_today_gets_no_pause(self):
        """Picked up today, not yet dumped -- not paused, just mid-trip."""
        movement = self._full_cuba_movement(self.today)

        TransactionPauseService.generate_due_pauses(today=self.today)

        self.assertFalse(AssetTransactionPause.objects.filter(movement=movement).exists())

    def test_running_twice_never_duplicates(self):
        movement = self._full_cuba_movement(self.yesterday)

        TransactionPauseService.generate_due_pauses(today=self.today)
        second_run_created = TransactionPauseService.generate_due_pauses(today=self.today)

        self.assertEqual(len(second_run_created), 0)
        self.assertEqual(
            AssetTransactionPause.objects.filter(movement=movement).count(), 1,
        )

    def test_a_long_pause_still_only_gets_one_record(self):
        """A cuba parked for a week is still just one pause, not one per
        night -- the daily task would call this once a day, every day,
        while it's pending; simulate that with repeated calls on later days."""
        movement = self._full_cuba_movement(self.yesterday)

        TransactionPauseService.generate_due_pauses(today=self.today)
        TransactionPauseService.generate_due_pauses(today=self.today + datetime.timedelta(days=5))
        TransactionPauseService.generate_due_pauses(today=self.today + datetime.timedelta(days=10))

        self.assertEqual(
            AssetTransactionPause.objects.filter(movement=movement).count(), 1,
        )

    def test_superseded_by_a_later_pickup_is_not_a_candidate(self):
        """Dropped off and picked up again (a genuine second MOVEMENT) --
        that second pickup already gets its own fresh di_number on its own,
        nothing to do for the first one here."""
        first_movement = self._full_cuba_movement(self.yesterday)
        self._full_cuba_movement(self.today)  # a later pickup for the same asset

        TransactionPauseService.generate_due_pauses(today=self.today)

        self.assertFalse(
            AssetTransactionPause.objects.filter(movement=first_movement).exists()
        )

    def test_non_full_cuba_movement_is_never_a_candidate(self):
        empty_movement = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=_aware(self.yesterday),
            data={"cubaEstado": "Cuba vacía"},
        )

        TransactionPauseService.generate_due_pauses(today=self.today)

        self.assertFalse(
            AssetTransactionPause.objects.filter(movement=empty_movement).exists()
        )

    def test_resolves_correctly_even_against_a_split_receipt_family(self):
        """movement_transaction on a split's siblings all point back to the
        same movement -- any of them is enough to read the resolution date."""
        movement = self._full_cuba_movement(self.yesterday)
        receipt = self._receipt_for(movement, self.today)
        receipt.split_into_parts(2)

        created = TransactionPauseService.generate_due_pauses(today=self.today)

        self.assertEqual(len(created), 1)

    def test_detection_does_not_grow_queries_with_more_candidates(self):
        """Resolving each candidate's receipt/company must come from the
        prefetch, not a query per movement."""
        for _ in range(5):
            movement = self._full_cuba_movement(self.yesterday)
            self._receipt_for(movement, self.yesterday)  # already resolved, same day

        with self.assertNumQueries(2):
            self.assertEqual(TransactionPauseService.generate_due_pauses(today=self.today), [])


class TransactionPauseDateFieldTests(TestCase):
    """date is hand-editable by a superuser only (see
    AssetTransactionPauseAdmin.get_readonly_fields), same rule as di_number
    everywhere else -- but it's a real field a new pause defaults on its
    own, not something only assigned once like di_number."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.asset_type = make_asset_type(cls.company)
        cls.asset = make_asset(cls.company, cls.asset_type)
        cls.superuser = User.objects.create_superuser(username="super", email="s@s.com", password="x")
        cls.staff = User.objects.create_user(username="staff", password="x", is_staff=True)

    def _request(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return request

    def test_defaults_to_its_movements_own_date(self):
        movement = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=timezone.now(),
            data={"cubaEstado": "Cuba llena"},
        )

        pause = AssetTransactionPause.objects.create(movement=movement)

        self.assertEqual(pause.date, movement.date)

    def test_superuser_can_edit_date(self):
        admin_instance = AssetTransactionPauseAdmin(AssetTransactionPause, AdminSite())

        fields = admin_instance.get_readonly_fields(self._request(self.superuser))

        self.assertNotIn("date", fields)

    def test_regular_staff_cannot_edit_date(self):
        admin_instance = AssetTransactionPauseAdmin(AssetTransactionPause, AdminSite())

        fields = admin_instance.get_readonly_fields(self._request(self.staff))

        self.assertIn("date", fields)

    def test_regular_staff_can_still_open_the_form(self):
        """date gets excluded from the generated form for a non-superuser --
        the custom __init__ must not assume it's always present (see the
        di_number KeyError this same mistake caused elsewhere)."""
        admin_instance = AssetTransactionPauseAdmin(AssetTransactionPause, AdminSite())
        form_class = admin_instance.get_form(self._request(self.staff))

        form = form_class()

        self.assertNotIn("date", form.fields)

    def test_date_uses_the_unfold_split_widget(self):
        form = AssetTransactionPauseAdminForm()

        self.assertIsInstance(form.fields["date"].widget, UnfoldAdminSplitDateTimeWidget)


class TransactionPauseResolutionDateTests(TestCase):
    """date must track the movement's resolving receipt (verteo/dejada), not
    stay pinned to the original pickup day -- a driver moving the cuba days
    later would otherwise see a stale date on its DI."""

    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.asset_type = make_asset_type(cls.company)
        cls.asset = make_asset(cls.company, cls.asset_type)

    def _movement(self, date):
        return AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.MOVEMENT, date=date,
            data={"cubaEstado": "Cuba llena"},
        )

    def _receipt(self, movement, date):
        receipt_tx = AssetTransaction.objects.create(
            company=self.company, asset=self.asset,
            transaction_type=TransactionTypeChoices.RECEIPT, date=date,
        )
        return AssetTransactionReceipt.objects.create(
            transaction=receipt_tx, nro_albaran="01083485", movement_transaction=movement,
        )

    def test_still_pending_defaults_to_the_movement_date(self):
        movement = self._movement(timezone.now() - datetime.timedelta(days=3))

        pause = AssetTransactionPause.objects.create(movement=movement)

        self.assertEqual(pause.date, movement.date)

    def test_already_resolved_when_created_picks_up_the_receipts_date(self):
        movement = self._movement(timezone.now() - datetime.timedelta(days=3))
        receipt = self._receipt(movement, timezone.now())

        pause = AssetTransactionPause.objects.create(movement=movement)

        self.assertEqual(pause.date, receipt.transaction.date)

    def test_pause_date_resyncs_once_the_receipt_arrives_later(self):
        """The common case this exists for: a pause gets created (daily
        task) while the movement is still pending, days before it's
        actually dumped -- filing that verteo must refresh the pause's date
        instead of leaving it on the stale pickup day."""
        movement = self._movement(timezone.now() - datetime.timedelta(days=5))
        pause = AssetTransactionPause.objects.create(movement=movement)
        self.assertEqual(pause.date, movement.date)

        receipt = self._receipt(movement, timezone.now())

        pause.refresh_from_db()
        self.assertEqual(pause.date, receipt.transaction.date)

    def test_overridden_date_is_never_resynced(self):
        """A superuser's manual correction sticks even once a (possibly
        later, possibly re-filed) receipt shows up."""
        movement = self._movement(timezone.now() - datetime.timedelta(days=5))
        pause = AssetTransactionPause.objects.create(movement=movement)
        corrected = timezone.now() - datetime.timedelta(days=1)
        pause.date = corrected
        pause.date_overridden = True
        pause.save()

        self._receipt(movement, timezone.now())

        pause.refresh_from_db()
        self.assertEqual(pause.date, corrected)

    def test_admin_save_model_flags_override_only_on_a_real_date_change(self):
        movement = self._movement(timezone.now() - datetime.timedelta(days=5))
        pause = AssetTransactionPause.objects.create(movement=movement)
        admin_instance = AssetTransactionPauseAdmin(AssetTransactionPause, AdminSite())
        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser(username="super2", email="s2@s.com", password="x")

        form = type("Form", (), {"changed_data": ["date"]})()
        admin_instance.save_model(request, pause, form, change=True)

        self.assertTrue(pause.date_overridden)

    def test_admin_save_model_does_not_flag_override_on_an_unrelated_change(self):
        movement = self._movement(timezone.now() - datetime.timedelta(days=5))
        pause = AssetTransactionPause.objects.create(movement=movement)
        admin_instance = AssetTransactionPauseAdmin(AssetTransactionPause, AdminSite())
        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser(username="super3", email="s3@s.com", password="x")

        form = type("Form", (), {"changed_data": []})()
        admin_instance.save_model(request, pause, form, change=True)

        self.assertFalse(pause.date_overridden)
