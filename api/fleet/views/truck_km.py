import json

from django.db.models import Q
from django.http import JsonResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from assets.models import AssetIdentifierLabel, AssetPlanning, TruckKmSession
from assets.services import truck_km
from assets.services.truck_km import TruckKmError
from company.utils import get_current_employee


def error_response(error: TruckKmError):
    return JsonResponse(error.as_payload(), status=error.status)


def truck_from_body(company, body, id_key="truck_id"):
    return truck_km.find_truck(company, truck_id=body.get(id_key), matricula=body.get("matricula"))


class _TruckKmView(APIView):
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.employee = get_current_employee(request.user)

    def body(self):
        try:
            return json.loads(self.request.body or b"{}")
        except json.JSONDecodeError:
            raise TruckKmError("invalid_json", "Invalid JSON")

    def handle_exception(self, exc):
        if isinstance(exc, TruckKmError):
            return error_response(exc)
        return super().handle_exception(exc)

    def require_employee(self):
        if self.employee is None:
            raise TruckKmError("no_employee", "No employee profile found")
        return self.employee


class TrucksListView(_TruckKmView):
    """Trucks for the picker: [{id, name, matricula, last_km}]. ?q= matches name or plate."""

    def get(self, request):
        employee = self.require_employee()
        trucks = truck_km.company_trucks(employee.company).order_by("name")
        q = request.query_params.get("q", "").strip()
        if q:
            trucks = trucks.filter(
                Q(name__icontains=q)
                | Q(identifiers__label__iexact=AssetIdentifierLabel.PLATE, identifiers__value__icontains=q)
            ).distinct()
        return JsonResponse({"trucks": [
            {**truck_km.truck_payload(truck), "last_km": TruckKmSession.last_km(truck)} for truck in trucks
        ]})


class TruckKmCurrentView(_TruckKmView):
    def get(self, request):
        employee = self.require_employee()
        return JsonResponse({"open_session": truck_km.session_payload(TruckKmSession.open_for(employee))})


class TruckKmStartView(_TruckKmView):
    def post(self, request):
        employee = self.require_employee()
        body = self.body()
        session = truck_km.start_session(
            employee, truck_from_body(employee.company, body), body.get("km"),
            confirm_lower=bool(body.get("confirm_lower")), confirm_takeover=bool(body.get("confirm_takeover")),
        )
        return JsonResponse({"success": True, "open_session": truck_km.session_payload(session)})


class TruckKmEndView(_TruckKmView):
    def post(self, request):
        employee = self.require_employee()
        body = self.body()
        truck_km.end_session(employee, body.get("km"), confirm_lower=bool(body.get("confirm_lower")))
        return JsonResponse({"success": True, "open_session": None})


class TruckKmSwitchView(_TruckKmView):
    """Cambio de camión: close the current truck and open the new one in one step."""

    def post(self, request):
        employee = self.require_employee()
        body = self.body()
        if body.get("end_km") is None and TruckKmSession.open_for(employee) is not None:
            raise TruckKmError("km_invalid", "Introduce los kilómetros finales del camión actual.")
        planning = None
        if body.get("planning_id"):
            planning = AssetPlanning.objects.filter(id=body["planning_id"], assigned_to=employee).first()
        session = truck_km.switch(
            employee, body.get("end_km"), truck_from_body(employee.company, body), body.get("start_km"),
            confirm_lower=bool(body.get("confirm_lower")), confirm_takeover=bool(body.get("confirm_takeover")),
            planning=planning,
        )
        return JsonResponse({"success": True, "open_session": truck_km.session_payload(session)})
