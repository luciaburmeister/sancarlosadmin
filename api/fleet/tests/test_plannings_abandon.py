"""
api/fleet/tests/test_plannings_abandon.py

Smoke tests for POST /plannings/abandon/ — skip the current task.
Detailed stage-derivation is covered by assets/tests/test_abandon_service.py.
"""
from datetime import date

from rest_framework.test import APIClient, APITestCase

from api.fleet.tests.helpers import make_company, make_employee, make_user
from assets.models import AbandonedStageChoices, AssetPlanning, PlanningStatus, PlanningType


class PlanningAbandonViewTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.client.force_authenticate(user=self.user)

    def _planning(self, **kwargs):
        defaults = dict(
            company=self.company, assigned_to=self.employee,
            scheduled_date=date.today(), planning_type=PlanningType.PORTE,
            status=PlanningStatus.PENDING,
        )
        defaults.update(kwargs)
        return AssetPlanning.objects.create(**defaults)

    def _post(self, payload):
        return self.client.post("/en/api/plannings/abandon/", payload, format="json")

    def test_requires_authentication(self):
        anon_client = APIClient()
        response = anon_client.post("/en/api/plannings/abandon/")
        self.assertEqual(response.status_code, 401)

    def test_requires_reason(self):
        planning = self._planning()
        response = self._post({"planning_id": planning.id})
        self.assertEqual(response.status_code, 400)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.PENDING)

    def test_requires_planning_id(self):
        response = self._post({"reason": "No puedo acceder"})
        self.assertEqual(response.status_code, 400)

    def test_abandons_only_the_given_planning(self):
        current = self._planning()
        later = self._planning()
        response = self._post({"planning_id": current.id, "reason": "No puedo acceder"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["id"], current.id)
        self.assertEqual(data["planning_type"], PlanningType.PORTE)
        self.assertEqual(data["abandoned_stage"], AbandonedStageChoices.NOT_STARTED)
        current.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual(current.status, PlanningStatus.NOT_COMPLETED)
        self.assertEqual(current.abandoned_reason, "No puedo acceder")
        self.assertEqual(later.status, PlanningStatus.PENDING)

    def test_unknown_planning_returns_400(self):
        response = self._post({"planning_id": 99999, "reason": "No puedo acceder"})
        self.assertEqual(response.status_code, 400)

    def test_blocked_when_carrying_a_picked_up_porte(self):
        carried = self._planning(status=PlanningStatus.IN_TRANSIT)
        response = self._post({"planning_id": carried.id, "reason": "No puedo acceder"})
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertEqual(data["blocked_planning_id"], carried.id)
        self.assertIn("error", data)
        carried.refresh_from_db()
        self.assertNotEqual(carried.status, PlanningStatus.NOT_COMPLETED)
