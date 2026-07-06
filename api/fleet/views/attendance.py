import base64
import json
from decimal import Decimal

from assets.models import AssetTransaction, TransactionTypeChoices
from company.models import WorkAttendance
from django.core.files.base import ContentFile
from django.http import JsonResponse
from django.utils import timezone
from company.utils import get_current_employee
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView


def save_image_to_field(transaction, field_name, b64_data, filename):
    """Decode a base64 string and save it to an ImageField on the transaction."""
    try:
        image_data = base64.b64decode(b64_data)
        image_file = ContentFile(image_data, name=filename)
        getattr(transaction, field_name).save(filename, image_file, save=True)
    except Exception:
        pass


class MovementsPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


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
            planning_type="cuba",
        ).select_related(
            "asset", "origin", "destination", "transport_asset"
        ).prefetch_related("stops__zone").order_by("route_order", "pk")

        data = []
        for p in plannings:
            stops = [
                {
                    "zone": s.zone.name,
                    "address": f"{s.zone.street_address}, {s.zone.city}, {s.zone.postal_code}",
                    "sequence": s.sequence,
                }
                for s in p.stops.all()
            ]
            stops = [
                {
                    "zone": s.zone.name,
                    "address": f"{s.zone.street_address}, {s.zone.city}, {s.zone.postal_code}",
                    "sequence": s.sequence,
                }
                for s in p.stops.all()
            ]
            data.append({
                "id": p.id,
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
                "stops": stops,
                "transport_asset": (
                    {"id": p.transport_asset.id, "name": p.transport_asset.name}
                    if p.transport_asset else None
                ),
                "route_order": p.route_order,
                "scheduled_date": p.scheduled_date.strftime("%d/%m/%Y"),
                "status": p.status,
            })

        return JsonResponse({"plannings": data})


class MovementsListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"count": 0, "next": None, "previous": None, "results": []})

        transactions = AssetTransaction.objects.filter(
            performed_by=employee,
        ).select_related("asset", "from_zone", "to_zone", "performed_by").order_by("-date")

        data = []
        for t in transactions:
            t_data = t.data if isinstance(t.data, dict) else {}
            data.append({
                "id": t.id,
                "asset": t.asset.name,
                "from_zone": t.from_zone.name if t.from_zone else None,
                "to_zone": t.to_zone.name if t.to_zone else None,
                "transaction_type": t.transaction_type,
                "date": t.date.strftime("%d/%m/%Y %H:%M"),
                "performed_by": t.performed_by.full_name if t.performed_by else None,
                "movement_type": t_data.get("movement_type", t_data.get("tipo", "cuba")),
            })

        paginator = MovementsPagination()
        page = paginator.paginate_queryset(data, request)
        return paginator.get_paginated_response(page)


class PortesPlanningListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from assets.models import AssetPlanning
        from api.fleet.views.assets import get_zone_coordinates

        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"transport_plannings": []})

        plannings = AssetPlanning.objects.filter(
            assigned_to=employee,
            status__in=["pending", "in_transit"],
            planning_type="porte",
        ).select_related("asset", "origin", "destination")

        data = []
        for p in plannings:
            origin_lat, origin_lon = get_zone_coordinates(p.origin) if p.origin else (None, None)
            dest_lat, dest_lon = get_zone_coordinates(p.destination) if p.destination else (None, None)

            data.append({
                "id": p.id,
                "asset_id": p.asset.id,
                "asset_name": p.asset.name,
                "load_description": p.load_description,
                "origin": p.origin.name if p.origin else None,
                "origin_address": (
                    f"{p.origin.street_address}, {p.origin.city}, {p.origin.postal_code}"
                    if p.origin else None
                ),
                "origin_lat": origin_lat,
                "origin_lon": origin_lon,
                "destination": p.destination.name if p.destination else None,
                "destination_address": (
                    f"{p.destination.street_address}, {p.destination.city}, {p.destination.postal_code}"
                    if p.destination else None
                ),
                "destination_lat": dest_lat,
                "destination_lon": dest_lon,
                "scheduled_date": p.scheduled_date.strftime("%d/%m/%Y"),
                "status": p.status,
                "notes": p.notes,
            })

        return JsonResponse({"transport_plannings": data})


class PorteSubmitView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, planning_id):
        from assets.models import AssetPlanning

        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found"}, status=400)

        try:
            raw = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)


        try:
            planning = AssetPlanning.objects.select_related(
                "asset", "origin", "destination"
            ).get(
                id=planning_id,
                assigned_to=employee,
                planning_type="porte",
            )
        except AssetPlanning.DoesNotExist:
            return JsonResponse({"error": "Planning not found"}, status=404)

        action = raw.get("action")
        if action not in ("recogida", "entrega"):
            return JsonResponse({"error": "action must be 'recogida' or 'entrega'"}, status=400)

        firma_b64 = raw.get("firma", "")
        imagen_b64 = raw.get("imagen", "")
        matricula = raw.get("matricula", "")
        client_id = raw.get("client_id")
        notes = raw.get("notes", "")

        transaction_type = (
            TransactionTypeChoices.PORTE_MOVEMENT if action == "recogida"
            else TransactionTypeChoices.PORTE_DELIVERY
)


        transaction = AssetTransaction.objects.create(
            asset=planning.asset,
            company=planning.asset.company,
            from_zone=planning.origin,
            to_zone=planning.destination,
            transaction_type=transaction_type,
            date=timezone.now(),
            performed_by=employee,
            notes=f"Porte {action} - Matricula: {matricula}. {notes}",
            data={
                "movement_type": "porte",
                "action": action,
                "matricula": matricula,
                "planning_id": planning_id,
                "load_description": planning.load_description,
                "client_id": client_id,
            }
        )

        if firma_b64:
            save_image_to_field(transaction, "firma_image", firma_b64, f"firma_porte_{planning_id}_{action}.png")

        if imagen_b64:
            save_image_to_field(transaction, "imagen", imagen_b64, f"porte_{planning_id}_{action}.jpg")

        if action == "recogida":
            planning.status = "in_transit"
        elif action == "entrega":
            planning.status = "delivered"
        planning.save()

        return JsonResponse({"success": True, "message": f"Porte {action} registered successfully"})