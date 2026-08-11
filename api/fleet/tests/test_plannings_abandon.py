"""
api/fleet/tests/test_plannings_abandon.py

Smoke tests for POST /plannings/abandon/ — the "No puedo hacer más" endpoint.
Detailed stage-derivation and scoping logic is covered by
assets/tests/test_abandon_service.py; this only checks the HTTP wiring.
"""
from datetime import date

from rest_framework.test import APIClient, APITestCase

from api.fleet.tests.helpers import make_company, make_employee, make_user
from assets.models import AbandonedStageChoices, AssetPlanning, PlanningStatus, PlanningType


class PlanningAbandonRemainingViewTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company()
        self.user = make_user()
        self.employee = make_employee(self.company, self.user)
        self.client.force_authenticate(user=self.user)

    def test_requires_authentication(self):
        anon_client = APIClient()
        response = anon_client.post("/en/api/plannings/abandon/")
        self.assertEqual(response.status_code, 401)

    def test_abandons_and_returns_summary(self):
        planning = AssetPlanning.objects.create(
            company=self.company, assigned_to=self.employee,
            scheduled_date=date.today(), planning_type=PlanningType.PORTE, status=PlanningStatus.PENDING,
        )
        response = self.client.post("/en/api/plannings/abandon/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["abandoned"][0]["id"], planning.id)
        self.assertEqual(data["abandoned"][0]["planning_type"], PlanningType.PORTE)
        self.assertEqual(data["abandoned"][0]["abandoned_stage"], AbandonedStageChoices.NOT_STARTED)
        planning.refresh_from_db()
        self.assertEqual(planning.status, PlanningStatus.NOT_COMPLETED)

    def test_no_open_plannings_returns_empty(self):
        response = self.client.post("/en/api/plannings/abandon/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"count": 0, "abandoned": []})
