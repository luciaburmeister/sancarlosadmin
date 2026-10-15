from datetime import date, timedelta

from django.test import TestCase
from django.utils import timezone

from api.fleet.serializers import CubaPlanningSerializer
from api.fleet.tests.helpers import make_asset, make_asset_type, make_company, make_employee, make_planning, make_user, make_zone
from assets.models import (
    AssetDataKey,
    AssetPlanning,
    AssetTransaction,
    CubaFillState,
    CubaSize,
    CubaTransactionKey,
    PlanningStatus,
    PlanningType,
    TransactionTypeChoices,
)
from assets.services.cuba_reservation import (
    CubaReservationConflict,
    find_candidates,
    release_stale_reservations,
    reserve_cuba,
)
from company.models import WorkingZone


def _make_empty_cuba(company, asset_type, zone, name="CUBA-001", capacidad=CubaSize.M3_10):
    asset = make_asset(company, asset_type, zone=zone, name=name)
    asset.data = {AssetDataKey.CAPACIDAD: capacidad}
    asset.save(update_fields=["data"])
    AssetTransaction.objects.create(
        company=company,
        asset=asset,
        transaction_type=TransactionTypeChoices.DELIVERY,
        date=timezone.now() - timedelta(hours=1),
        data={CubaTransactionKey.ESTADO: CubaFillState.EMPTY},
    )
    return asset


class CubaReservationFixtureMixin:
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.company = make_company()
        cls.user = make_user()
        cls.employee = make_employee(cls.company, cls.user)
        cls.asset_type = make_asset_type(cls.company)
        cls.destination = make_zone(cls.company, name="Obra Destino", lat=40.4168, lon=-3.7038)
        cls.destination.zone_type = WorkingZone.ZoneType.OBRA
        cls.destination.save(update_fields=["zone_type"])
        cls.almacen = make_zone(cls.company, name="Almacén", lat=40.4200, lon=-3.7000)
        cls.almacen.zone_type = WorkingZone.ZoneType.ALMACEN
        cls.almacen.save(update_fields=["zone_type"])


class CubaSizeMatchTests(TestCase):
    def test_matches_leading_digits_and_display_label(self):
        planning = AssetPlanning(cuba_size=CubaSize.M3_10)
        self.assertTrue(planning.matches_cuba_size("10"))
        self.assertTrue(planning.matches_cuba_size("10 M3"))
        self.assertTrue(planning.matches_cuba_size(CubaSize.M3_10))
        self.assertFalse(planning.matches_cuba_size(CubaSize.M3_20))

    def test_blank_plan_size_matches_any_capacidad(self):
        planning = AssetPlanning(cuba_size="")
        self.assertTrue(planning.matches_cuba_size(CubaSize.M3_30))
        self.assertTrue(planning.matches_cuba_size("10"))


class FindCandidatesTests(CubaReservationFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.far_almacen = make_zone(cls.company, name="Almacén Lejano", lat=41.3874, lon=2.1686)
        cls.far_almacen.zone_type = WorkingZone.ZoneType.ALMACEN
        cls.far_almacen.save(update_fields=["zone_type"])
        cls.planning = make_planning(
            cls.company, assigned_to=cls.employee,
            planning_type=PlanningType.CUBA, destination=cls.destination,
            cuba_size=CubaSize.M3_10,
        )

    def test_suggests_empty_cuba_at_warehouse(self):
        cuba = _make_empty_cuba(self.company, self.asset_type, self.almacen)
        self.assertEqual([c.asset for c in find_candidates(self.planning)], [cuba])

    def test_orders_by_distance_to_destination(self):
        near = _make_empty_cuba(self.company, self.asset_type, self.almacen, name="CUBA-NEAR")
        far = _make_empty_cuba(self.company, self.asset_type, self.far_almacen, name="CUBA-FAR")
        candidates = find_candidates(self.planning)
        self.assertEqual([c.asset for c in candidates], [near, far])
        self.assertLess(candidates[0].distance_m, candidates[1].distance_m)

    def test_excludes_cuba_not_at_a_warehouse(self):
        _make_empty_cuba(self.company, self.asset_type, self.destination)
        self.assertEqual(find_candidates(self.planning), [])

    def test_excludes_full_cuba(self):
        asset = make_asset(self.company, self.asset_type, zone=self.almacen, name="CUBA-FULL")
        asset.data = {AssetDataKey.CAPACIDAD: CubaSize.M3_10}
        asset.save(update_fields=["data"])
        AssetTransaction.objects.create(
            company=self.company, asset=asset, transaction_type=TransactionTypeChoices.DELIVERY,
            date=timezone.now(), data={CubaTransactionKey.ESTADO: CubaFillState.FULL},
        )
        self.assertEqual(find_candidates(self.planning), [])

    def test_excludes_cuba_currently_picked_up(self):
        asset = make_asset(self.company, self.asset_type, zone=self.almacen, name="CUBA-ON-TRUCK")
        asset.data = {AssetDataKey.CAPACIDAD: CubaSize.M3_10}
        asset.save(update_fields=["data"])
        AssetTransaction.objects.create(
            company=self.company, asset=asset, transaction_type=TransactionTypeChoices.MOVEMENT,
            date=timezone.now(), data={},
        )
        self.assertEqual(find_candidates(self.planning), [])

    def test_excludes_wrong_size(self):
        _make_empty_cuba(self.company, self.asset_type, self.almacen, capacidad=CubaSize.M3_20)
        self.assertEqual(find_candidates(self.planning), [])

    def test_matches_bare_digit_capacidad_entered_in_the_field(self):
        cuba = _make_empty_cuba(self.company, self.asset_type, self.almacen, capacidad="10")
        self.assertEqual([c.asset for c in find_candidates(self.planning)], [cuba])

    def test_matches_any_size_when_planning_cuba_size_is_blank(self):
        planning = make_planning(
            self.company, assigned_to=self.employee, planning_type=PlanningType.CUBA,
            destination=self.destination, cuba_size="",
        )
        cuba = _make_empty_cuba(self.company, self.asset_type, self.almacen, capacidad=CubaSize.M3_30)
        self.assertEqual([c.asset for c in find_candidates(planning)], [cuba])

    def test_matches_size_via_display_label(self):
        cuba = _make_empty_cuba(self.company, self.asset_type, self.almacen, capacidad="10 M3")
        self.assertEqual([c.asset for c in find_candidates(self.planning)], [cuba])

    def test_excludes_cuba_already_reserved_by_another_open_planning_today(self):
        cuba = _make_empty_cuba(self.company, self.asset_type, self.almacen)
        other_driver = make_employee(self.company, make_user(username="other"))
        make_planning(
            self.company, assigned_to=other_driver, planning_type=PlanningType.CUBA,
            destination=self.destination, cuba_size=CubaSize.M3_10, reserved_asset=cuba,
        )
        self.assertEqual(find_candidates(self.planning), [])

    def test_excludes_cuba_already_claimed_as_asset_by_another_open_planning_today(self):
        cuba = _make_empty_cuba(self.company, self.asset_type, self.almacen)
        other_driver = make_employee(self.company, make_user(username="other"))
        make_planning(
            self.company, assigned_to=other_driver, planning_type=PlanningType.CUBA,
            destination=self.destination, cuba_size=CubaSize.M3_10, asset=cuba,
        )
        self.assertEqual(find_candidates(self.planning), [])

    def test_does_not_exclude_cuba_locked_by_a_not_completed_planning(self):
        cuba = _make_empty_cuba(self.company, self.asset_type, self.almacen)
        other_driver = make_employee(self.company, make_user(username="other"))
        make_planning(
            self.company, assigned_to=other_driver, planning_type=PlanningType.CUBA,
            destination=self.destination, cuba_size=CubaSize.M3_10, reserved_asset=cuba,
            status=PlanningStatus.NOT_COMPLETED,
        )
        self.assertEqual([c.asset for c in find_candidates(self.planning)], [cuba])

    def test_does_not_exclude_reserved_cuba_once_that_plan_scanned_a_different_one(self):
        reserved = _make_empty_cuba(self.company, self.asset_type, self.almacen, name="CUBA-RESERVED")
        scanned = _make_empty_cuba(self.company, self.asset_type, self.almacen, name="CUBA-SCANNED")
        other_driver = make_employee(self.company, make_user(username="other"))
        make_planning(
            self.company, assigned_to=other_driver, planning_type=PlanningType.CUBA,
            destination=self.destination, cuba_size=CubaSize.M3_10,
            reserved_asset=reserved, asset=scanned,
        )
        candidates = [c.asset for c in find_candidates(self.planning)]
        self.assertIn(reserved, candidates)
        self.assertNotIn(scanned, candidates)

    def test_excludes_cuba_manually_excluded_by_the_planning_truck(self):
        cuba = _make_empty_cuba(self.company, self.asset_type, self.almacen)
        truck = make_asset(self.company, self.asset_type, name="TRUCK-01")
        truck.excluded_assets.add(cuba)
        self.planning.transport_asset = truck
        self.planning.save(update_fields=["transport_asset"])
        self.assertEqual(find_candidates(self.planning), [])

    def test_does_not_exclude_cuba_unrelated_to_the_planning_truck(self):
        cuba = _make_empty_cuba(self.company, self.asset_type, self.almacen)
        truck = make_asset(self.company, self.asset_type, name="TRUCK-01")
        other_truck = make_asset(self.company, self.asset_type, name="TRUCK-02")
        other_truck.excluded_assets.add(cuba)
        self.planning.transport_asset = truck
        self.planning.save(update_fields=["transport_asset"])
        self.assertEqual([c.asset for c in find_candidates(self.planning)], [cuba])


class ReserveCubaTests(CubaReservationFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cuba = make_asset(cls.company, cls.asset_type, zone=cls.almacen, name="CUBA-001")
        cls.cuba.data = {AssetDataKey.CAPACIDAD: CubaSize.M3_10}
        cls.cuba.save(update_fields=["data"])
        cls.planning = make_planning(
            cls.company, assigned_to=cls.employee, planning_type=PlanningType.CUBA,
            destination=cls.destination, cuba_size=CubaSize.M3_10,
        )

    def setUp(self):
        self.planning.refresh_from_db()

    def test_reserve_sets_reserved_asset_not_asset(self):
        planning = reserve_cuba(self.planning, self.cuba)
        self.assertEqual(planning.reserved_asset_id, self.cuba.id)
        self.assertIsNone(planning.asset_id)

    def test_reserve_raises_conflict_when_already_reserved_by_another_plan(self):
        other_driver = make_employee(self.company, make_user(username="other"))
        make_planning(
            self.company, assigned_to=other_driver, planning_type=PlanningType.CUBA,
            destination=self.destination, cuba_size=CubaSize.M3_10, reserved_asset=self.cuba,
        )
        with self.assertRaises(CubaReservationConflict) as ctx:
            reserve_cuba(self.planning, self.cuba)
        self.assertEqual(ctx.exception.remaining_candidates, [])


class CancelReservationTests(CubaReservationFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cuba = make_asset(cls.company, cls.asset_type, zone=cls.almacen, name="CUBA-001")
        cls.planning = make_planning(
            cls.company, assigned_to=cls.employee, planning_type=PlanningType.CUBA,
            reserved_asset=cls.cuba,
        )

    def setUp(self):
        self.planning.refresh_from_db()
        if self.planning.reserved_asset_id != self.cuba.id:
            self.planning.reserved_asset = self.cuba
            self.planning.save(update_fields=["reserved_asset"])

    def test_clears_reserved_asset(self):
        planning = self.planning.cancel_cuba_reservation()
        self.assertIsNone(planning.reserved_asset_id)
        self.planning.refresh_from_db()
        self.assertIsNone(self.planning.reserved_asset_id)

    def test_is_a_no_op_when_nothing_was_reserved(self):
        self.planning.reserved_asset = None
        self.planning.save(update_fields=["reserved_asset"])
        planning = self.planning.cancel_cuba_reservation()
        self.assertIsNone(planning.reserved_asset_id)

    def test_frees_the_cuba_for_other_plans_immediately(self):
        self.planning.cancel_cuba_reservation()
        other_planning = make_planning(
            self.company, assigned_to=self.employee, planning_type=PlanningType.CUBA,
            destination=None, cuba_size="",
        )
        self.cuba.data = {AssetDataKey.CAPACIDAD: CubaSize.M3_10}
        self.cuba.save(update_fields=["data"])
        self.assertIn(self.cuba, [c.asset for c in find_candidates(other_planning)])


class ReleaseReservationIfScannedOtherTests(TestCase):
    def test_clears_when_scanned_asset_differs(self):
        planning = AssetPlanning(reserved_asset_id=1)
        scanned = type("Asset", (), {"pk": 2})()
        self.assertTrue(planning.release_reservation_if_scanned_other(scanned))
        self.assertIsNone(planning.reserved_asset)

    def test_keeps_when_scanned_asset_is_the_reserved_one(self):
        planning = AssetPlanning(reserved_asset_id=1)
        scanned = type("Asset", (), {"pk": 1})()
        self.assertFalse(planning.release_reservation_if_scanned_other(scanned))
        self.assertEqual(planning.reserved_asset_id, 1)


class ReleaseStaleReservationsTests(CubaReservationFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cuba = make_asset(cls.company, cls.asset_type, zone=cls.almacen, name="CUBA-001")

    def test_clears_reservation_from_a_past_day_but_not_today(self):
        stale = make_planning(
            self.company, assigned_to=self.employee, planning_type=PlanningType.CUBA,
            reserved_asset=self.cuba, scheduled_date=date.today() - timedelta(days=1),
        )
        current = make_planning(
            self.company, assigned_to=self.employee, planning_type=PlanningType.CUBA,
            reserved_asset=self.cuba, scheduled_date=date.today(),
        )
        released_ids = release_stale_reservations()
        stale.refresh_from_db()
        current.refresh_from_db()
        self.assertEqual(released_ids, [stale.id])
        self.assertIsNone(stale.reserved_asset_id)
        self.assertEqual(current.reserved_asset_id, self.cuba.id)


class CubaPlanningSerializerTests(CubaReservationFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cuba = _make_empty_cuba(cls.company, cls.asset_type, cls.almacen)
        cls.planning = make_planning(
            cls.company, assigned_to=cls.employee, planning_type=PlanningType.CUBA,
            destination=cls.destination, cuba_size=CubaSize.M3_10, reserved_asset=cls.cuba,
        )

    def test_unscanned_plan_exposes_reserved_cuba_and_zero_asset_id(self):
        data = CubaPlanningSerializer(self.planning).data
        self.assertEqual(data["asset_id"], 0)
        self.assertIsNone(data["asset_name"])
        self.assertEqual(data["reserved_asset_id"], self.cuba.id)
        self.assertEqual(data["reserved_asset_name"], self.cuba.name)
        self.assertEqual(data["reserved_asset_zone_name"], self.almacen.name)
        self.assertEqual(data["cuba_size"], CubaSize.M3_10)
        self.assertEqual(data["planning_type"], PlanningType.CUBA)
