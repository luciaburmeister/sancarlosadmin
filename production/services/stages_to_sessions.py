from django.core.exceptions import ValidationError
from django.utils import timezone

from production.models import (
    ProductionItem,
    ProductionItemStatus,
    ProductionItemSession,
    ProductionItemSessionStatus,
    ProductionItemSessionKind,
)
from company.models import Employee
from production.exceptions import StageHasActiveWorkersError
from django.utils.translation import gettext_lazy as _
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse



import unicodedata


class StagesToSessionsService:
    @classmethod
    def _worker_has_open_session(cls, employee: Employee) -> bool:
        """A worker can only have one active session at a time globally."""
        return ProductionItemSession.objects.filter(
            created_by=employee,
            status=ProductionItemSessionStatus.IN_PROGRESS,
            end_time__isnull=True,
        ).exists()

    @classmethod
    def _stage_has_open_session(cls, item: ProductionItem, stage_name: str) -> bool:
        """Only one worker can have an active session per item+stage at a time."""
        return ProductionItemSession.objects.filter(
            production_item=item,
            production_stage=stage_name,
            end_time__isnull=True,
        ).exists()

    @classmethod
    def _close_open_sessions_for_stage(cls, item: ProductionItem, stage_name: str) -> None:
        """Close rows that still have end_time=NULL for this item+stage.

        Required by UniqueConstraint unique_open_session_per_employee_item_stage.
        Reverting planning to pending does not remove the session created on start;
        without this, the next start_activity would try to insert a second open row.
        """
        now = timezone.now()
        qs = ProductionItemSession.objects.filter(
            production_item=item,
            production_stage=stage_name,
            end_time__isnull=True,
        )
        for sess in qs:
            sess.end_time = now
            sess.status = ProductionItemSessionStatus.CANCELLED
            sess.save(update_fields=["end_time", "status", "updated_at"])

    @classmethod
    def _uses_shared_stage_activities(cls, items: list[ProductionItem]) -> bool:
        """Shared vs exclusive stage rules follow the production item's company."""
        if not items or not items[0].company_id:
            return True
        return items[0].company.allow_shared_production_stage_activities

    @classmethod
    def start_activity(cls, *, stage_name: str, items: list[ProductionItem], employee: Employee) -> int:
        if cls._uses_shared_stage_activities(items):
            return cls.start_group_activity(
                stage_name=stage_name, items=items, employee=employee
            )
        return cls._start_exclusive_activity(
            stage_name=stage_name, items=items, employee=employee
        )

    @classmethod
    def start_group_activity(
        cls, *, stage_name: str, items: list[ProductionItem], employee: Employee
    ) -> int:
        """Multiple workers may join the same stage while it is in progress."""
        stage_name = unicodedata.normalize("NFC", stage_name)
        if employee is None:
            raise ValidationError("No employee provided to start activity.")

        # Rule: worker cannot already be active on something else
        if cls._worker_has_open_session(employee):
            raise ValidationError("This worker already has an active session elsewhere.")

        now = timezone.now()
        count = 0

        for item in items:
            current_stage, stage_info = cls._get_current_stage(item, requested_stage=stage_name)
            if current_stage != stage_name:
                continue

            status = (stage_info.get("status") or ProductionItemStatus.PENDING)
            if status not in (ProductionItemStatus.PENDING, ProductionItemStatus.IN_PROGRESS):
                continue

            already_active = ProductionItemSession.objects.filter(
                production_item=item,
                production_stage=stage_name,
                created_by=employee,
                end_time__isnull=True,
            ).exists()
            if already_active:
                continue

            if status == ProductionItemStatus.PENDING:
                cls._close_open_sessions_for_stage(item, stage_name)
                stage_info["status"] = ProductionItemStatus.IN_PROGRESS
                stage_info["started_at"] = stage_info.get("started_at") or now.isoformat()
                stage_info["completed_at"] = None
                cls._save_planning(item, stage_name, stage_info)

            ProductionItemSession.objects.create(
                production_item=item,
                production_stage=stage_name,
                status=ProductionItemSessionStatus.IN_PROGRESS,
                start_time=now,
                created_by=employee,
                kind=ProductionItemSessionKind.ACTIVITY_STARTED,
            )
            count += 1
        return count

    @classmethod
    def _start_exclusive_activity(
        cls, *, stage_name: str, items: list[ProductionItem], employee: Employee
    ) -> int:
        """Only one worker per stage; resuming a paused (in_progress, no active session) stage is allowed."""
        stage_name = unicodedata.normalize("NFC", stage_name)
        if employee is None:
            raise ValidationError("No employee provided to start activity.")

        now = timezone.now()
        count = 0

        for item in items:
            current_stage, stage_info = cls._get_current_stage(item, requested_stage=stage_name)
            if current_stage != stage_name:
                continue

            status = (stage_info.get("status") or ProductionItemStatus.PENDING)
            if status == ProductionItemStatus.IN_PROGRESS:
                # Allow resuming a paused stage only if no other worker has an open session.
                if cls._stage_has_open_session(item, stage_name):
                    continue
            elif status != ProductionItemStatus.PENDING:
                continue

            already_active = ProductionItemSession.objects.filter(
                production_item=item,
                production_stage=stage_name,
                created_by=employee,
                end_time__isnull=True,
            ).exists()
            if already_active:
                continue

            cls._close_open_sessions_for_stage(item, stage_name)
            stage_info["status"] = ProductionItemStatus.IN_PROGRESS
            stage_info["started_at"] = stage_info.get("started_at") or now.isoformat()
            stage_info["completed_at"] = None
            cls._save_planning(item, stage_name, stage_info)

            ProductionItemSession.objects.create(
                production_item=item,
                production_stage=stage_name,
                status=ProductionItemSessionStatus.IN_PROGRESS,
                start_time=now,
                created_by=employee,
                kind=ProductionItemSessionKind.ACTIVITY_STARTED,
            )
            count += 1
        return count

    @classmethod
    def _open_sessions_for_stage(cls, item: ProductionItem, stage_name: str):
        return list(
            ProductionItemSession.objects.filter(
                production_item=item,
                production_stage=stage_name,
                status=ProductionItemSessionStatus.IN_PROGRESS,
                end_time__isnull=True,
            ).select_related("created_by")
        )

    @classmethod
    def _other_active_workers_on_stage(
        cls, item: ProductionItem, stage_name: str, employee: Employee
    ) -> list[dict]:
        """Employees (other than *employee*) with an open session on this item+stage."""
        seen: set[int] = set()
        workers: list[dict] = []
        for sess in cls._open_sessions_for_stage(item, stage_name):
            emp = sess.created_by
            if emp is None or emp.id == employee.id or emp.id in seen:
                continue
            seen.add(emp.id)
            workers.append({"id": emp.id, "full_name": emp.full_name})
        return workers

    @classmethod
    def finish_activity(cls, *, stage_name: str, items: list[ProductionItem], employee: Employee) -> int:
        stage_name = unicodedata.normalize("NFC", stage_name)
        now = timezone.now()
        count = 0

        for item in items:
            current_stage, stage_info = cls._get_current_stage(item, requested_stage=stage_name)
            if current_stage != stage_name:
                continue

            status = (stage_info.get("status") or ProductionItemStatus.PENDING)
            if status != ProductionItemStatus.IN_PROGRESS:
                continue

            others = cls._other_active_workers_on_stage(item, stage_name, employee)
            if others:
                raise StageHasActiveWorkersError(others)

            for sess in cls._open_sessions_for_stage(item, stage_name):
                if sess.created_by_id != employee.id:
                    continue
                sess.kind = ProductionItemSessionKind.ACTIVITY_FINISHED
                sess.finish(completed_by=employee)

            stage_info["status"] = ProductionItemStatus.DONE
            stage_info["started_at"] = stage_info.get("started_at") or now.isoformat()
            stage_info["completed_at"] = now.isoformat()
            cls._save_planning(item, stage_name, stage_info)
            count += 1
        return count

    @classmethod
    def start_session(cls, *, stage_name: str, items: list[ProductionItem], employee: Employee) -> int:
        stage_name = unicodedata.normalize("NFC", stage_name)
        now = timezone.now()
        count = 0

        if cls._worker_has_open_session(employee):
            raise ValidationError(_("You already have an active session elsewhere. End it before starting a new one."))

        for item in items:
            current_stage, stage_info = cls._get_current_stage(item, requested_stage=stage_name)
            if current_stage != stage_name:
                continue

            status = (stage_info.get("status") or ProductionItemStatus.PENDING)
            if status != ProductionItemStatus.IN_PROGRESS:
                continue

            already_active = ProductionItemSession.objects.filter(
                production_item=item,
                production_stage=stage_name,
                created_by=employee,
                status=ProductionItemSessionStatus.IN_PROGRESS,
                end_time__isnull=True,
            ).exists()
            if already_active:
                continue

            if cls._stage_has_open_session(item, stage_name):
                raise ValidationError(_("Another worker already has an active session on this stage. Wait for them to pause first."))

            ProductionItemSession.objects.create(
                production_item=item,
                production_stage=stage_name,
                status=ProductionItemSessionStatus.IN_PROGRESS,
                start_time=now,
                created_by=employee,
                kind=ProductionItemSessionKind.SESSION_STARTED,
            )
            count += 1
        return count

    @classmethod
    def leave_activity(cls, *, stage_name: str, items: list[ProductionItem], employee: Employee) -> int:
        """Close this worker's open session without completing the stage for everyone."""
        stage_name = unicodedata.normalize("NFC", stage_name)
        count = 0
        for item in items:
            session = (
                ProductionItemSession.objects.filter(
                    production_item=item,
                    production_stage=stage_name,
                    created_by=employee,
                    status=ProductionItemSessionStatus.IN_PROGRESS,
                    end_time__isnull=True,
                )
                .order_by("-start_time")
                .first()
            )
            if not session:
                continue

            if session.kind == ProductionItemSessionKind.ACTIVITY_STARTED:
                session.kind = ProductionItemSessionKind.ACTIVITY_FINISHED
            elif session.kind == ProductionItemSessionKind.SESSION_STARTED:
                session.kind = ProductionItemSessionKind.SESSION_FINISHED
            session.finish(completed_by=employee)
            count += 1

        if count == 0:
            raise ValidationError(_("No tienes una sesión activa en esta etapa."))
        return count

    @classmethod
    def end_session(cls, *, stage_name: str, items: list[ProductionItem], employee: Employee) -> int:
        stage_name = unicodedata.normalize("NFC", stage_name)
        count = 0
        for item in items:
            session = (
                ProductionItemSession.objects.filter(
                    production_item=item,
                    production_stage=stage_name,
                    created_by=employee,
                    status=ProductionItemSessionStatus.IN_PROGRESS,
                    end_time__isnull=True,
                )
                .order_by("-start_time")
                .first()
            )
            if not session:
                raise ValidationError(
                    _("No active session found to end. It may have already been closed.")
                )

            session.kind = ProductionItemSessionKind.SESSION_FINISHED
            session.finish(completed_by=employee)
            count += 1
        return count

    @classmethod
    def revert_activity(
        cls,
        *,
        stage_name: str,
        items: list[ProductionItem],
        target_status: str = ProductionItemStatus.IN_PROGRESS,
    ) -> int:
        """Revert a completed stage back to in_progress or pending.

        target_status must be 'in_progress' or 'pending'.
        Open ProductionItemSession rows for this stage are closed (cancelled) so
        planning and sessions stay aligned and a new start_activity can run.
        Finished sessions from prior completions remain in the audit trail.
        """
        if target_status not in (ProductionItemStatus.IN_PROGRESS, ProductionItemStatus.PENDING):
            raise ValidationError(
                f"Invalid target_status '{target_status}'. Must be 'in_progress' or 'pending'."
            )

        stage_name = unicodedata.normalize("NFC", stage_name)
        count = 0

        # Which source statuses are valid depends on the target:
        # → pending: accept in_progress or done (can always go back fully)
        # → in_progress: only accept done (undo a finish)
        valid_sources = (
            {ProductionItemStatus.IN_PROGRESS, ProductionItemStatus.DONE}
            if target_status == ProductionItemStatus.PENDING
            else {ProductionItemStatus.DONE}
        )

        for item in items:
            planning = {unicodedata.normalize("NFC", k): v for k, v in (item.planning or {}).items()}
            if stage_name not in planning:
                continue

            stage_info = planning.get(stage_name) or {}
            current_status = stage_info.get("status") or ProductionItemStatus.PENDING
            if current_status not in valid_sources:
                continue

            cls._close_open_sessions_for_stage(item, stage_name)

            stage_info["status"] = target_status
            stage_info["completed_at"] = None
            # Reverting to pending also clears the start timestamp
            if target_status == ProductionItemStatus.PENDING:
                stage_info["started_at"] = None
            cls._save_planning(item, stage_name, stage_info)
            count += 1

        return count

    @classmethod
    def _get_current_stage(cls, production_item: ProductionItem, requested_stage: str | None = None):
        if requested_stage:
            requested_stage = unicodedata.normalize("NFC", requested_stage)

        planning = production_item.planning or {}
        # Normalize planning keys for comparison
        planning = {unicodedata.normalize("NFC", k): v for k, v in planning.items()}

        ticket = production_item.ticket
        config = ticket.source_object
        product = getattr(config, "product", None)
        stage_names = list(getattr(product, "production_stages", []) or [])
        # Normalize known stage names
        stage_names = [unicodedata.normalize("NFC", s) for s in stage_names]

        if not stage_names:
            return None, None

        if requested_stage in stage_names and requested_stage in planning:
            info = planning.get(requested_stage) or {}
            if (info.get("status") or ProductionItemStatus.PENDING) != ProductionItemStatus.DONE:
                return requested_stage, info

        for name in stage_names:
            if name not in planning:
                continue
            info = planning.get(name) or {}
            if (info.get("status") or ProductionItemStatus.PENDING) == ProductionItemStatus.IN_PROGRESS:
                return name, info

        for name in stage_names:
            if name not in planning:
                continue
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
    def _save_planning(cls, production_item: ProductionItem, stage_name: str, stage_info: dict):
        planning = {unicodedata.normalize("NFC", k): v for k, v in (production_item.planning or {}).items()}
        planning[stage_name] = stage_info

        production_item.planning = planning
        production_item.status = cls._compute_item_status(planning)
        production_item.save(update_fields=["planning", "status", "updated_at"])