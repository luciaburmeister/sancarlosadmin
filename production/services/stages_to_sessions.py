from django.core.exceptions import ValidationError
from django.utils import timezone

from production.models import (
    ProductionItem,
    ProductionItemStatus,
    ProductionItemSession,
    ProductionItemSessionStatus,
)
from company.models import Employee


class StagesToSessionsService:

    @classmethod
    def start_activity(cls, *, production_item: ProductionItem, employee: Employee) -> None:
        if employee is None:
            raise ValidationError("No employee provided to start activity.")

        stage_name, stage_info = cls._get_current_stage(production_item)
        if stage_name is None:
            return

        status = stage_info.get("status") or ProductionItemStatus.PENDING
        if status != ProductionItemStatus.PENDING:
            return

        now = timezone.now()

        stage_info["status"] = ProductionItemStatus.IN_PROGRESS
        stage_info["started_at"] = stage_info.get("started_at") or now.isoformat()
        stage_info["completed_at"] = None

        cls._save_planning(production_item, stage_name, stage_info)

        ProductionItemSession.objects.create(
            production_item=production_item,
            production_stage=stage_name,
            status=ProductionItemSessionStatus.IN_PROGRESS,
            start_time=now,
            created_by=employee,
        )

    @classmethod
    def finish_activity(cls, *, production_item: ProductionItem, employee: Employee) -> None:
        stage_name, stage_info = cls._get_current_stage(production_item)
        if stage_name is None:
            return

        status = stage_info.get("status") or ProductionItemStatus.PENDING
        if status != ProductionItemStatus.IN_PROGRESS:
            return

        session = (
            ProductionItemSession.objects
            .filter(
                production_item=production_item,
                production_stage=stage_name,
                created_by=employee,
                status=ProductionItemSessionStatus.IN_PROGRESS,
            )
            .order_by("-start_time")
            .first()
        )

        if session:
            session.comments = "Activity Finished"
            session.finish(completed_by=employee)
            session.save(update_fields=["comments", "end_time", "status", "updated_at"])


        now = timezone.now()
        stage_info["status"] = ProductionItemStatus.DONE
        stage_info["started_at"] = stage_info.get("started_at") or now.isoformat()
        stage_info["completed_at"] = now.isoformat()

        cls._save_planning(production_item, stage_name, stage_info)

    @classmethod
    def start_session(cls, *, production_item: ProductionItem, employee: Employee) -> None:
        stage_name, stage_info = cls._get_current_stage(production_item)
        if stage_name is None:
            return

        status = stage_info.get("status") or ProductionItemStatus.PENDING

        if status != ProductionItemStatus.IN_PROGRESS:
            raise ValidationError("Start the activity before starting a session.")

        already_active = ProductionItemSession.objects.filter(
            production_item=production_item,
            production_stage=stage_name,
            created_by=employee,
            status=ProductionItemSessionStatus.IN_PROGRESS,
        ).exists()
        if already_active:
            return

        now = timezone.now()
        ProductionItemSession.objects.create(
            production_item=production_item,
            production_stage=stage_name,
            status=ProductionItemSessionStatus.IN_PROGRESS,
            start_time=now,
            created_by=employee,
        )

    @classmethod
    def end_session(cls, *, production_item: ProductionItem, employee: Employee) -> None:
        stage_name, stage_info = cls._get_current_stage(production_item)
        if stage_name is None:
            return

        session = (
            ProductionItemSession.objects.filter(
                production_item=production_item,
                production_stage=stage_name,
                created_by=employee,
                status=ProductionItemSessionStatus.IN_PROGRESS,
            )
            .order_by("-start_time")
            .first()
        )
        if not session:
            return

        session.comments = "Session Finished"
        session.finish(completed_by=employee)
        session.save(update_fields=["comments", "end_time", "status", "updated_at"])


    @staticmethod
    def _get_current_stage(production_item: ProductionItem):
        planning = production_item.planning or {}
        stage_names = list(planning.keys())
        if not stage_names:
            return None, None

        for name in stage_names:
            info = planning.get(name) or {}
            if (info.get("status") or ProductionItemStatus.PENDING) == ProductionItemStatus.IN_PROGRESS:
                return name, info

        for name in stage_names:
            info = planning.get(name) or {}
            if (info.get("status") or ProductionItemStatus.PENDING) != ProductionItemStatus.DONE:
                return name, info

        return None, None

    @staticmethod
    def _compute_item_status(planning: dict):
        if not planning:
            return ProductionItemStatus.PENDING

        statuses = [(info or {}).get("status") or ProductionItemStatus.PENDING for info in planning.values()]

        if any(s == ProductionItemStatus.IN_PROGRESS for s in statuses):
            return ProductionItemStatus.IN_PROGRESS
        if all(s == ProductionItemStatus.DONE for s in statuses):
            return ProductionItemStatus.DONE
        if any(s == ProductionItemStatus.CANCELLED for s in statuses):
            return ProductionItemStatus.CANCELLED

        return ProductionItemStatus.PENDING

    @classmethod
    def _save_planning(cls, production_item: ProductionItem, stage_name: str, stage_info: dict) -> None:
        planning = production_item.planning or {}
        planning[stage_name] = stage_info

        production_item.planning = planning
        production_item.status = cls._compute_item_status(planning)
        production_item.save(update_fields=["planning", "status", "updated_at"])
