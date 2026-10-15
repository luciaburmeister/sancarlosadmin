from django.urls import reverse
from rest_framework.test import APIClient, APITestCase

from api.fleet.tests.helpers import (
    make_almacen,
    make_asset,
    make_asset_type,
    make_company,
    make_employee,
    make_obra,
    make_planning,
    make_user,
)
from assets.constants import AssignRequestKey
from assets.models import (
    AssetDataKey,
    AssetTransaction,
    CubaFillState,
    CubaSize,
    CubaTransactionKey,
    PlanningType,
    TransactionTypeChoices,
)


class CubaReservationApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company()
        cls.user = make_user()
        cls.employee = make_employee(cls.company, cls.user)
        cls.asset_type = make_asset_type(cls.company)

        cls.destination = make_obra(cls.company)
        cls.almacen = make_almacen(cls.company, name="Almacén")

        cls.cuba = make_asset(cls.company, cls.asset_type, zone=cls.almacen, name="CUBA-001")
        cls.cuba.data = {AssetDataKey.CAPACIDAD: CubaSize.M3_10}
        cls.cuba.save(update_fields=["data"])
        AssetTransaction.objects.create(
            company=cls.company, asset=cls.cuba, transaction_type=TransactionTypeChoices.DELIVERY,
            data={CubaTransactionKey.ESTADO: CubaFillState.EMPTY},
        )

        cls.planning = make_planning(
            cls.company, assigned_to=cls.employee, planning_type=PlanningType.CUBA,
            destination=cls.destination, cuba_size=CubaSize.M3_10,
        )

    def setUp(self):
        self.client = APIClient()
        self.planning.refresh_from_db()
        if self.planning.reserved_asset_id is not None:
            self.planning.reserved_asset = None
            self.planning.save(update_fields=["reserved_asset"])

    def suggestions_url(self, planning=None):
        return reverse("cuba_suggestions", args=[(planning or self.planning).id])

    def reserve_url(self, planning=None):
        return reverse("reserve_cuba", args=[(planning or self.planning).id])

    def test_suggestions_lists_candidate_cuba(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.suggestions_url())
        self.assertEqual(response.status_code, 200)
        candidates = response.json()["candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["asset_id"], self.cuba.id)
        self.assertEqual(candidates[0]["zone_name"], "Almacén")

    def test_suggestions_404_for_someone_elses_planning(self):
        other = make_employee(self.company, make_user(username="other"))
        other_planning = make_planning(
            self.company, assigned_to=other, planning_type=PlanningType.CUBA,
            destination=self.destination, cuba_size=CubaSize.M3_10,
        )
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.suggestions_url(other_planning))
        self.assertEqual(response.status_code, 404)

    def test_reserve_sets_reserved_asset(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            self.reserve_url(), {AssignRequestKey.ASSET_ID: self.cuba.id}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["reserved_asset_id"], self.cuba.id)
        self.planning.refresh_from_db()
        self.assertEqual(self.planning.reserved_asset_id, self.cuba.id)
        self.assertIsNone(self.planning.asset_id)

    def test_reserve_conflict_returns_409_with_remaining_candidates(self):
        other = make_employee(self.company, make_user(username="other"))
        make_planning(
            self.company, assigned_to=other, planning_type=PlanningType.CUBA,
            destination=self.destination, cuba_size=CubaSize.M3_10, reserved_asset=self.cuba,
        )
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            self.reserve_url(), {AssignRequestKey.ASSET_ID: self.cuba.id}, format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["candidates"], [])

    def test_reserve_404_for_asset_in_another_company(self):
        other_company = make_company(name="Other Co")
        other_asset = make_asset(other_company, make_asset_type(other_company), name="CUBA-OTHER")
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            self.reserve_url(), {AssignRequestKey.ASSET_ID: other_asset.id}, format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_cancel_clears_reservation(self):
        self.planning.reserved_asset = self.cuba
        self.planning.save(update_fields=["reserved_asset"])
        self.client.force_authenticate(user=self.user)
        response = self.client.delete(self.reserve_url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.planning.refresh_from_db()
        self.assertIsNone(self.planning.reserved_asset_id)

    def test_cancel_404_for_someone_elses_planning(self):
        other = make_employee(self.company, make_user(username="other"))
        other_planning = make_planning(
            self.company, assigned_to=other, planning_type=PlanningType.CUBA,
            destination=self.destination, cuba_size=CubaSize.M3_10, reserved_asset=self.cuba,
        )
        self.client.force_authenticate(user=self.user)
        response = self.client.delete(self.reserve_url(other_planning))
        self.assertEqual(response.status_code, 404)
