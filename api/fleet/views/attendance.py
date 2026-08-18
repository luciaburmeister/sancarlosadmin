import base64
import json
from decimal import Decimal
from datetime import datetime
from assets.models import AssetTransaction, PlanningStatus, TransactionTypeChoices
from company.models import WorkAttendance
from django.core.files.base import ContentFile
from django.http import JsonResponse
from django.utils import timezone
from company.utils import get_current_employee
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView


def save_image_to_field(transaction, field_name, b64_data, filename):
    try:
        image_data = base64.b64decode(b64_data)
        image_file = ContentFile(image_data, name=filename)
        getattr(transaction, field_name).save(filename, image_file, save=True)
    except Exception:
        pass


def _parse_dt(iso_str):
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except Exception:
        return None


class MovementsPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class AttendanceClockInView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            data = json.loads(request.body)
            latitude = data.get("latitude")
            longitude = data.get("longitude")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found for this user"}, status=400)
        existing = WorkAttendance.objects.filter(employee=employee, clock_out_time__isnull=True).first()
        if existing:
            return JsonResponse({"error": "Ya has fichado entrada. Ficha salida primero."}, status=400)
        attendance = WorkAttendance.objects.create(
            employee=employee, company=employee.company, clock_in_time=timezone.now(),
            clock_in_lat=Decimal(str(latitude)) if latitude else None,
            clock_in_lng=Decimal(str(longitude)) if longitude else None,
        )
        return JsonResponse({"success": True, "message": "Entrada registrada", "attendance_id": attendance.id, "clock_in_time": attendance.clock_in_time.strftime("%d/%m/%Y %H:%M:%S")})


class AttendanceClockOutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            data = json.loads(request.body)
            latitude = data.get("latitude")
            longitude = data.get("longitude")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found for this user"}, status=400)
        attendance = WorkAttendance.objects.filter(employee=employee, clock_out_time__isnull=True).first()
        if not attendance:
            return JsonResponse({"error": "No hay fichaje de entrada activo."}, status=400)
        attendance.clock_out_time = timezone.now()
        attendance.clock_out_lat = Decimal(str(latitude)) if latitude else None
        attendance.clock_out_lng = Decimal(str(longitude)) if longitude else None
        attendance.save()
        return JsonResponse({"success": True, "message": "Salida registrada", "clock_out_time": attendance.clock_out_time.strftime("%d/%m/%Y %H:%M:%S")})


class AttendanceStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"is_clocked_in": False})
        is_clocked_in = WorkAttendance.objects.filter(employee=employee, clock_out_time__isnull=True).exists()
        return JsonResponse({"is_clocked_in": is_clocked_in})


def _serialize_cuba_planning(p):
    from api.fleet.views.assets import get_zone_coordinates
    origin_lat, origin_lon = get_zone_coordinates(p.origin) if p.origin else (None, None)
    dest_lat, dest_lon = get_zone_coordinates(p.destination) if p.destination else (None, None)
    stops = [{"zone": s.zone.name, "address": f"{s.zone.street_address}, {s.zone.city}, {s.zone.postal_code}", "sequence": s.sequence} for s in p.stops.all()]
    return {
        "id": p.id, "planning_type": p.planning_type, "asset_id": p.asset.id if p.asset else 0,
        "asset_name": p.asset.name if p.asset else None,
        "origin": p.origin.name if p.origin else None,
        "origin_address": f"{p.origin.street_address}, {p.origin.city}, {p.origin.postal_code}" if p.origin else None,
        "origin_lat": origin_lat, "origin_lon": origin_lon,
        "destination": p.destination.name if p.destination else None,
        "destination_address": f"{p.destination.street_address}, {p.destination.city}, {p.destination.postal_code}" if p.destination else None,
        "destination_lat": dest_lat, "destination_lon": dest_lon,
        "stops": stops,
        "vehicle": {"id": p.transport_asset.id, "name": p.transport_asset.name} if p.transport_asset else None,
        "route_order": p.route_order,
        "scheduled_date": p.scheduled_date.strftime("%d/%m/%Y"),
        "status": p.status,
        "notes": p.notes,
        "service_type": p.service_type,
        "service_type_label": p.get_service_type_display() if p.service_type else None,
        "cuba_operation_type": p.cuba_operation_type,
        "cuba_operation_type_label": p.get_cuba_operation_type_display() if p.cuba_operation_type else None,
        "cuba_size": p.cuba_size,
        "cuba_size_label": p.get_cuba_size_display() if p.cuba_size else None,
    }


def _serialize_porte_planning(p, scannable_asset_id=None):
    from api.fleet.views.assets import get_zone_coordinates
    origin_lat, origin_lon = get_zone_coordinates(p.origin) if p.origin else (None, None)
    dest_lat, dest_lon = get_zone_coordinates(p.destination) if p.destination else (None, None)
    display_asset = p.asset or p.transport_asset
    if scannable_asset_id is None:
        scannable_asset_id = display_asset.id if display_asset else 0
    return {
        "id": p.id, "planning_type": p.planning_type, "asset_id": scannable_asset_id, "asset_name": display_asset.name if display_asset else "",
        "load_description": p.load_description,
        "origin": p.origin.name if p.origin else None,
        "origin_address": f"{p.origin.street_address}, {p.origin.city}, {p.origin.postal_code}" if p.origin else None,
        "origin_lat": origin_lat, "origin_lon": origin_lon,
        "destination": p.destination.name if p.destination else None,
        "destination_address": f"{p.destination.street_address}, {p.destination.city}, {p.destination.postal_code}" if p.destination else None,
        "destination_lat": dest_lat, "destination_lon": dest_lon,
        "scheduled_date": p.scheduled_date.strftime("%d/%m/%Y"),
        "status": p.status, "notes": p.notes,
        "vehicle": {"id": p.transport_asset.id, "name": p.transport_asset.name} if p.transport_asset else None,
        "service_type": p.service_type,
        "service_type_label": p.get_service_type_display() if p.service_type else None,
    }


class PlanningsListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"plannings": []})
        from assets.models import AssetPlanning, PlanningStatus, PlanningType
        plannings = AssetPlanning.objects.filter(
            assigned_to=employee, status__in=[PlanningStatus.PENDING, PlanningStatus.IN_TRANSIT], planning_type=PlanningType.CUBA,
        ).select_related("asset", "origin", "destination", "transport_asset").prefetch_related("stops__zone").order_by("route_order", "pk")
        data = [_serialize_cuba_planning(p) for p in plannings]
        return JsonResponse({"plannings": data})


class PlanningNextTaskView(APIView):
    """The single next cuba/porte job for this driver — cuba and porte share one combined
    route_order sequence, so only one task is ever surfaced at a time, regardless of type.

    Scoped to today only: a plan left open from a previous day (missed, delayed, never
    resolved) must not leak into today's queue, and a future day's plan must not be
    revealed early. Stale open plans from past days are dispatch's problem to reassign
    via the schedule — see AssetPlanning.abandoned_stage / the "horario" admin view."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"task": None})
        from assets.models import AssetPlanning, PlanningStatus, PlanningType
        planning = AssetPlanning.objects.filter(
            assigned_to=employee, status__in=[PlanningStatus.PENDING, PlanningStatus.IN_TRANSIT],
            planning_type__in=[PlanningType.CUBA, PlanningType.PORTE],
            scheduled_date=timezone.now().date(),
        ).select_related(
            "asset", "origin", "destination", "transport_asset",
        ).prefetch_related("stops__zone").order_by("scheduled_date", "route_order", "scheduled_time", "pk").first()
        if planning is None:
            return JsonResponse({"task": None})
        task = (
            _serialize_cuba_planning(planning) if planning.planning_type == PlanningType.CUBA
            else _serialize_porte_planning(planning)
        )
        return JsonResponse({"task": task})


class PlanningAbandonRemainingView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found"}, status=400)
        from assets.services.abandon import PlanningAbandoner
        abandoned = PlanningAbandoner().abandon_remaining(employee)
        return JsonResponse({
            "count": len(abandoned),
            "abandoned": [
                {
                    "id": p.id, "planning_type": p.planning_type,
                    "abandoned_stage": p.abandoned_stage,
                    "abandoned_stage_label": p.get_abandoned_stage_display(),
                }
                for p in abandoned
            ],
        })


class MovementsListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"count": 0, "next": None, "previous": None, "results": []})
        transactions = AssetTransaction.objects.filter(
            performed_by=employee,
        ).exclude(
            transaction_type__in=[TransactionTypeChoices.MACHINE_HOURS, TransactionTypeChoices.LABORER_HOURS],
        ).select_related("asset", "from_zone", "to_zone", "performed_by").order_by("-date")
        data = []
        for t in transactions:
            t_data = t.data if isinstance(t.data, dict) else {}
            data.append({
                "id": t.id,
                "asset": t.asset.name if t.asset else None,
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


class MachineHoursHistorialView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"count": 0, "next": None, "previous": None, "results": []})

        transactions = AssetTransaction.objects.filter(
            performed_by=employee,
            transaction_type=TransactionTypeChoices.MACHINE_HOURS,
            data__has_key='machine_end_time',
        ).select_related("asset", "asset__asset_type", "from_zone").order_by("-data__machine_start_time")

        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            transactions = transactions.filter(data__machine_start_time__gte=date_from)
        if date_to:
            transactions = transactions.filter(data__machine_start_time__lte=date_to + "T23:59:59.999999")

        data = []
        for t in transactions:
            t_data = t.data if isinstance(t.data, dict) else {}
            intervals = [{"start": iv.get("start"), "end": iv.get("end")} for iv in t_data.get("intervals", [])]
            start_dt = _parse_dt(t_data.get("machine_start_time"))
            end_dt = _parse_dt(t_data.get("machine_end_time"))
            data.append({
                "id": t.id,
                "asset_name": t.asset.name if t.asset else "",
                "asset_type": t.asset.asset_type.name if t.asset and t.asset.asset_type else "",
                "obra": str(t.from_zone) if t.from_zone else "",
                "date": start_dt.strftime("%d/%m/%Y") if start_dt else "",
                "start_time": start_dt.strftime("%H:%M") if start_dt else None,
                "end_time": end_dt.strftime("%H:%M") if end_dt else None,
                "intervals": intervals,
            })

        paginator = MovementsPagination()
        page = paginator.paginate_queryset(data, request)
        return paginator.get_paginated_response(page)


class PortesPlanningListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from assets.models import AssetPlanning, PlanningStatus, PlanningType
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"transport_plannings": []})
        plannings = AssetPlanning.objects.filter(
            assigned_to=employee, status__in=[PlanningStatus.PENDING, PlanningStatus.IN_TRANSIT], planning_type=PlanningType.PORTE,
        ).select_related("asset", "origin", "destination", "transport_asset").order_by("scheduled_time", "pk")
        data = []
        # Same truck, several portes: only the earliest is scannable via QR.
        claimed_scannable_asset_ids = set()
        for p in plannings:
            display_asset = p.asset or p.transport_asset
            scannable_asset_id = 0
            if display_asset is not None and display_asset.id not in claimed_scannable_asset_ids:
                claimed_scannable_asset_ids.add(display_asset.id)
                scannable_asset_id = display_asset.id
            data.append(_serialize_porte_planning(p, scannable_asset_id=scannable_asset_id))
        return JsonResponse({"transport_plannings": data})


class PorteSubmitView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, planning_id):
        from assets.models import AssetPlanning, PlanningType
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found"}, status=400)
        try:
            raw = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)
        try:
            planning = AssetPlanning.objects.select_related(
                "asset", "transport_asset", "origin", "destination",
            ).get(
                id=planning_id, assigned_to=employee, planning_type=PlanningType.PORTE
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
        transaction_type = TransactionTypeChoices.PORTE_MOVEMENT if action == "recogida" else TransactionTypeChoices.PORTE_DELIVERY

        tx_asset = planning.asset or planning.transport_asset
        transaction = AssetTransaction.objects.create(
            asset=tx_asset, company=planning.company,
            from_zone=planning.origin, to_zone=planning.destination,
            transaction_type=transaction_type, date=timezone.now(), performed_by=employee,
            planning=planning,
            notes=f"Porte {action} - Matricula: {matricula}. {notes}",
            data={"movement_type": "porte", "action": action, "matricula": matricula, "planning_id": planning_id, "load_description": planning.load_description, "client_id": client_id}
        )
        if firma_b64:
            save_image_to_field(transaction, "firma_image", firma_b64, f"firma_porte_{planning_id}_{action}.png")
        if imagen_b64:
            save_image_to_field(transaction, "imagen", imagen_b64, f"porte_{planning_id}_{action}.jpg")
        planning.status = PlanningStatus.IN_TRANSIT if action == "recogida" else PlanningStatus.DELIVERED
        planning.save()
        return JsonResponse({"success": True, "message": f"Porte {action} registered successfully"})