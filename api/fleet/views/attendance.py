import base64
import json
from decimal import Decimal
from datetime import datetime
from assets.models import (
    Asset, AssetPlanning, AssetTransaction, PlanningStatus, PlanningType,
    TransactionTypeChoices, DRIVER_TASK_PLANNING_TYPES,
    PorteAction, PorteTransactionDataKey, PorteMovementKind,
    PORTE_ACTION_TRANSACTION_TYPES,
)
from company.models import WorkAttendance
from django.core.files.base import ContentFile
from django.http import JsonResponse
from django.utils import timezone
from company.utils import get_current_employee
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.serializers import BaseSerializer
from rest_framework.views import APIView

from api.fleet.serializers import CubaPlanningSerializer, PortePlanningSerializer


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


class PlanningsListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"plannings": []})
        from assets.models import AssetPlanning, PlanningStatus, PlanningType
        plannings = AssetPlanning.objects.filter(
            assigned_to=employee, status__in=[PlanningStatus.PENDING, PlanningStatus.IN_TRANSIT], planning_type=PlanningType.CUBA,
            scheduled_time__isnull=False,
        ).select_related(
            "asset", "reserved_asset", "reserved_asset__current_working_zone",
            "origin", "destination", "transport_asset",
        ).prefetch_related("stops__zone").order_by("scheduled_time", "route_order", "pk")
        plannings = list(plannings)
        AssetPlanning.attach_destination_pickups(plannings)
        return JsonResponse({"plannings": CubaPlanningSerializer(plannings, many=True).data})


_TASK_SERIALIZERS = {
    PlanningType.CUBA: CubaPlanningSerializer,
    PlanningType.PORTE: PortePlanningSerializer,
}


class PlanningNextTaskView(APIView):
    """The single next cuba/porte job for this driver — cuba and porte share one combined
    queue, so only one task is ever surfaced at a time, regardless of type. Ordered by
    scheduled_time to always match what dispatch sees in the "horario" admin view
    (assets/views/asset_schedule.py), with route_order only as a tiebreaker for jobs
    scheduled at the same time.

    Scoped to today only: a plan left open from a previous day (missed, delayed, never
    resolved) must not leak into today's queue, and a future day's plan must not be
    revealed early. Stale open plans from past days are dispatch's problem to reassign
    via the schedule — see AssetPlanning.abandoned_stage / the "horario" admin view.

    To support a new planning type, register its serializer in _TASK_SERIALIZERS above
    and add the type to DRIVER_TASK_PLANNING_TYPES in assets/models.py."""
    permission_classes = [IsAuthenticated]

    @staticmethod
    def _pending_cuba_dropoff(employee, planning=None):
        return Asset.objects.visible_pending_dropoff(employee, planning)

    def get(self, request):
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"task": None, "pending_dropoff": None})
        today = timezone.localdate()
        today_jobs = AssetPlanning.objects.filter(
            assigned_to=employee,
            planning_type__in=DRIVER_TASK_PLANNING_TYPES,
            scheduled_date=today,
            scheduled_time__isnull=False,
        ).select_related(
            "asset", "reserved_asset", "reserved_asset__current_working_zone",
            "origin", "destination", "transport_asset",
        ).prefetch_related("stops__zone")
        planning = today_jobs.filter(
            status=PlanningStatus.IN_TRANSIT,
        ).order_by("scheduled_time", "route_order", "pk").first()
        if planning is None:
            planning = today_jobs.filter(
                status=PlanningStatus.PENDING,
            ).order_by("scheduled_time", "route_order", "pk").first()

        pending_dropoff = self._pending_cuba_dropoff(employee, planning)
        if pending_dropoff is not None:
            pending_dropoff["can_use_for_next_task"] = (
                planning.can_claim_pending_cuba_for_next_task(pending_dropoff)
                if planning is not None else False
            )

        if planning is None:
            return JsonResponse({"task": None, "pending_dropoff": pending_dropoff})
        task = self._serialize_task(planning)
        if task is None:
            return JsonResponse({"error": f"No serializer registered for planning type '{planning.planning_type}'."}, status=500)
        return JsonResponse({"task": task, "pending_dropoff": pending_dropoff})

    @staticmethod
    def _serialize_task(planning):
        registered = _TASK_SERIALIZERS.get(planning.planning_type)
        if registered is None:
            return None
        if isinstance(registered, type) and issubclass(registered, BaseSerializer):
            return registered(planning).data
        return registered(planning)


class PlanningAbandonView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found"}, status=400)
        try:
            body = json.loads(request.body or b"{}")
        except (ValueError, TypeError):
            body = {}
        reason = (body.get("reason") or "").strip()
        if not reason:
            return JsonResponse({"error": "El motivo es obligatorio."}, status=400)
        planning_id = body.get("planning_id")
        if not planning_id:
            return JsonResponse({"error": "Debes indicar la tarea."}, status=400)
        today = timezone.localdate()
        planning = AssetPlanning.objects.filter(
            id=planning_id,
            assigned_to=employee,
            status__in=[PlanningStatus.PENDING, PlanningStatus.IN_TRANSIT],
            planning_type__in=DRIVER_TASK_PLANNING_TYPES,
            scheduled_date=today,
        ).first()
        if planning is None:
            return JsonResponse(
                {"error": "El plan indicado no existe o no está asignado a ti."},
                status=400,
            )
        from assets.services.abandon import PlanningAbandonBlockedError, PlanningAbandoner
        try:
            abandoned = PlanningAbandoner().abandon(employee, planning, reason=reason)
        except PlanningAbandonBlockedError as exc:
            label = exc.planning.get_planning_type_display()
            return JsonResponse({
                "error": (
                    f"Tienes un {label.lower()} recogido sin dejar. "
                    "Complétalo antes de cancelar esta tarea."
                ),
                "blocked_planning_id": exc.planning.id,
            }, status=400)
        return JsonResponse({
            "id": abandoned.id,
            "planning_type": abandoned.planning_type,
            "abandoned_stage": abandoned.abandoned_stage,
            "abandoned_stage_label": abandoned.get_abandoned_stage_display(),
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
                "movement_type": t_data.get("movement_type", t_data.get("tipo", "")),
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
            scheduled_time__isnull=False,
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
            data.append(
                PortePlanningSerializer(p, context={"scannable_asset_id": scannable_asset_id}).data
            )
        return JsonResponse({"transport_plannings": data})


class PorteSubmitView(APIView):
    """Driver porte flow: pickup empty cuba → load → deliver. No fill-state tracking."""
    permission_classes = [IsAuthenticated]

    def post(self, request, planning_id):
        from assets.constants import SERVICE_TYPE_MAQUINA_PROPIA
        from assets.models import AssetPlanning, AssetTypeCategory, PlanningType
        employee = get_current_employee(request.user)
        if employee is None:
            return JsonResponse({"error": "No employee profile found"}, status=400)
        try:
            raw = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)
        try:
            planning = AssetPlanning.objects.select_related(
                "asset", "machine", "transport_asset", "origin", "destination",
            ).get(
                id=planning_id, assigned_to=employee, planning_type=PlanningType.PORTE
            )
        except AssetPlanning.DoesNotExist:
            return JsonResponse({"error": "Planning not found"}, status=404)
        action = raw.get("action")
        if action not in PORTE_ACTION_TRANSACTION_TYPES:
            valid = ", ".join(PorteAction.values)
            return JsonResponse({"error": f"action must be one of: {valid}"}, status=400)
        firma_b64 = raw.get("firma", "")
        imagen_b64 = raw.get("imagen", "")
        matricula = raw.get("matricula", "")
        client_id = raw.get("client_id")
        notes = raw.get("notes", "")
        goods_type = raw.get("goods_type", "")
        tonnes_transported = raw.get("tonnes_transported", "")
        package_count = raw.get("package_count", "")
        reference_number = raw.get("reference_number", "")
        is_machine_porte = planning.service_type == SERVICE_TYPE_MAQUINA_PROPIA
        transaction_type = PORTE_ACTION_TRANSACTION_TYPES[action]

        tx_asset = planning.asset or planning.transport_asset
        if action == PorteAction.RECOGIDA:
            asset_id_from_scan = raw.get("asset_id")
            if asset_id_from_scan:
                try:
                    scanned_asset = Asset.objects.get(id=asset_id_from_scan, company=planning.company)
                    planning.asset = scanned_asset
                    planning.save(update_fields=["asset"])
                    tx_asset = scanned_asset
                except (Asset.DoesNotExist, ValueError):
                    return JsonResponse({"error": "El activo escaneado no pertenece a tu empresa."}, status=400)
            if planning.asset_id:
                planning.mark_cuba_claimed_by_driver()
        elif action == PorteAction.CARGA and is_machine_porte:
            machine_id_from_scan = raw.get("asset_id")
            if not machine_id_from_scan:
                return JsonResponse({"error": "Debes escanear el QR de la máquina."}, status=400)
            try:
                scanned_machine = Asset.objects.get(
                    id=machine_id_from_scan, company=planning.company,
                    asset_type__category=AssetTypeCategory.MACHINE,
                )
            except (Asset.DoesNotExist, ValueError):
                return JsonResponse({"error": "El activo escaneado no es una máquina de tu empresa."}, status=400)
            if planning.machine_id and scanned_machine.id != planning.machine_id:
                return JsonResponse(
                    {"error": "La máquina escaneada no coincide con la asignada al plan."},
                    status=400,
                )
            planning.machine = scanned_machine
            planning.save(update_fields=["machine"])
        elif action == PorteAction.ENTREGA and is_machine_porte:
            if not planning.transactions.filter(
                transaction_type=TransactionTypeChoices.PORTE_LOAD,
            ).exists():
                return JsonResponse({"error": "Debes escanear la máquina antes de entregar."}, status=400)
            if planning.machine_id:
                tx_asset = planning.machine

        transaction = AssetTransaction.objects.create(
            asset=tx_asset, company=planning.company,
            from_zone=planning.origin, to_zone=planning.destination,
            transaction_type=transaction_type, date=timezone.now(), performed_by=employee,
            planning=planning,
            notes=f"Porte {action} - Matricula: {matricula}. {notes}",
            data={
                PorteTransactionDataKey.MOVEMENT_TYPE: PorteMovementKind.PORTE,
                PorteTransactionDataKey.ACTION: action,
                PorteTransactionDataKey.MATRICULA: matricula,
                PorteTransactionDataKey.PLANNING_ID: planning_id,
                PorteTransactionDataKey.LOAD_DESCRIPTION: planning.load_description,
                PorteTransactionDataKey.CLIENT_ID: client_id,
                PorteTransactionDataKey.MACHINE_ID: planning.machine_id,
                PorteTransactionDataKey.GOODS_TYPE: goods_type,
                PorteTransactionDataKey.TONNES_TRANSPORTED: tonnes_transported,
                PorteTransactionDataKey.PACKAGE_COUNT: package_count,
                PorteTransactionDataKey.REFERENCE_NUMBER: reference_number,
            },
        )
        if firma_b64:
            save_image_to_field(transaction, "firma_image", firma_b64, f"firma_porte_{planning_id}_{action}.png")
        if imagen_b64:
            save_image_to_field(transaction, "imagen", imagen_b64, f"porte_{planning_id}_{action}.jpg")
        if action == PorteAction.ENTREGA:
            planning.status = PlanningStatus.DELIVERED
        elif planning.status != PlanningStatus.DELIVERED:
            planning.status = PlanningStatus.IN_TRANSIT
        planning.save()
        return JsonResponse({
            "success": True,
            "message": f"Porte {action} registered successfully",
            "transaction_id": transaction.id,
            "document_url": transaction.document_url,
        })