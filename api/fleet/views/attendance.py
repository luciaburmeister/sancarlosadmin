import json
from decimal import Decimal

from assets.models import AssetTransaction
from company.models import WorkAttendance
from django.http import JsonResponse
from django.utils import timezone
from company.utils import get_current_employee
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView


class AttendanceClockInView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        try:
            data = json.loads(request.body)
            latitude = data.get("latitude")
            longitude = data.get("longitude")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        employee = get_current_employee(user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found for this user"}, status=400)

        existing = WorkAttendance.objects.filter(
            employee=employee,
            clock_out_time__isnull=True,
        ).first()

        if existing:
            return JsonResponse({"error": "Ya has fichado entrada. Ficha salida primero."}, status=400)

        attendance = WorkAttendance.objects.create(
            employee=employee,
            company=employee.company,
            clock_in_time=timezone.now(),
            clock_in_lat=Decimal(str(latitude)) if latitude else None,
            clock_in_lng=Decimal(str(longitude)) if longitude else None,
        )

        return JsonResponse({
            "success": True,
            "message": "Entrada registrada",
            "attendance_id": attendance.id,
            "clock_in_time": attendance.clock_in_time.strftime("%d/%m/%Y %H:%M:%S"),
        })


class AttendanceClockOutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        try:
            data = json.loads(request.body)
            latitude = data.get("latitude")
            longitude = data.get("longitude")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        employee = get_current_employee(user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found for this user"}, status=400)

        attendance = WorkAttendance.objects.filter(
            employee=employee,
            clock_out_time__isnull=True,
        ).first()

        if not attendance:
            return JsonResponse({"error": "No hay fichaje de entrada activo."}, status=400)

        attendance.clock_out_time = timezone.now()
        attendance.clock_out_lat = Decimal(str(latitude)) if latitude else None
        attendance.clock_out_lng = Decimal(str(longitude)) if longitude else None
        attendance.save()

        return JsonResponse({
            "success": True,
            "message": "Salida registrada",
            "clock_out_time": attendance.clock_out_time.strftime("%d/%m/%Y %H:%M:%S"),
        })


class AttendanceStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        employee = get_current_employee(user)
        if employee is None:
            return JsonResponse({"is_clocked_in": False})

        is_clocked_in = WorkAttendance.objects.filter(
            employee=employee,
            clock_out_time__isnull=True,
        ).exists()

        return JsonResponse({"is_clocked_in": is_clocked_in})


class PlanningsListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        employee = get_current_employee(user)
        if employee is None:
            return JsonResponse({"plannings": []})

        from assets.models import AssetPlanning

        plannings = AssetPlanning.objects.filter(
            assigned_to=employee,
            status__in=["pending", "in_transit"],
        ).select_related("asset", "origin", "destination")

        data = []
        for p in plannings:
            data.append({
                "asset_id": p.asset.id,
                "origin": p.origin.name if p.origin else None,
                "origin_address": (
                    f"{p.origin.street_address}, {p.origin.city}, {p.origin.postal_code}"
                    if p.origin else None
                ),
                "destination": p.destination.name if p.destination else None,
                "destination_address": (
                    f"{p.destination.street_address}, {p.destination.city}, {p.destination.postal_code}"
                    if p.destination else None
                ),
                "scheduled_date": p.scheduled_date.strftime("%d/%m/%Y"),
                "status": p.status,
            })

        return JsonResponse({"plannings": data})


class MovementsListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        employee = get_current_employee(user)
        if employee is None:
            return JsonResponse({"movements": []})

        # Only show movements performed by this worker
        transactions = AssetTransaction.objects.filter(
            performed_by=employee,
        ).select_related("asset", "from_zone", "to_zone", "performed_by").order_by("-date")[:100]

        data = []
        for t in transactions:
            data.append({
                "id": t.id,
                "asset": t.asset.name,
                "from_zone": t.from_zone.name if t.from_zone else None,
                "to_zone": t.to_zone.name if t.to_zone else None,
                "transaction_type": t.transaction_type,
                "date": t.date.strftime("%d/%m/%Y %H:%M"),
                "performed_by": t.performed_by.full_name if t.performed_by else None,
            })

        return JsonResponse({"movements": data})