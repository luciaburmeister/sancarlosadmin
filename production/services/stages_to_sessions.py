from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from production.models import (
    ProductionItem,
    ProductionItemStatus,
    ProductionItemSession,
    ProductionItemSessionStatus,
    ProductionItemSessionKind,
)
from company.models import Employee
from production.constants import ProductionActions
from production.exceptions import StageHasActiveWorkersError, WorkerBusyError
from django.utils.translation import gettext_lazy as _

import unicodedata


class StagesToSessionsService:
    @classmethod
    def _ensure_worker_is_free(cls, employee: Employee) -> None:
        """A worker can only have one active session at a time globally."""
        session = (
            ProductionItemSession.objects.open_for(employee)
            .select_related("production_item")
            .order_by("-start_time")
            .first()
        )
        if session is not None:
            raise WorkerBusyError(session)

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
    def _validated_pause_reason(cls, items: list[ProductionItem], pause_reason: str | None) -> str | None:
        """Match a requested pause reason against the company catalogue.

        Returns the canonical value from Company.pause_reasons (comparison is
        NFC-normalized) or None when no reason was requested.
        """
        if not pause_reason:
            return None
        if not items or not items[0].company_id:
            raise ValidationError(_("No se puede validar el motivo de pausa sin empresa."))
        requested = unicodedata.normalize("NFC", pause_reason.strip())
        for reason in items[0].company.pause_reasons or []:
            if unicodedata.normalize("NFC", reason) == requested:
                return reason
        raise ValidationError(_("Motivo de pausa no válido para esta empresa."))

    @classmethod
    def _set_pause_reason(cls, item: ProductionItem, stage_name: str, reason: str) -> None:
        """Record why an in_progress stage was left paused (no open sessions)."""
        planning = {unicodedata.normalize("NFC", k): v for k, v in (item.planning or {}).items()}
        stage_info = planning.get(stage_name) or {}
        if (stage_info.get("status") or ProductionItemStatus.PENDING) != ProductionItemStatus.IN_PROGRESS:
            return
        stage_info["pause_reason"] = reason
        cls._save_planning(item, stage_name, stage_info)

    @classmethod
    def start_activity(cls, *, stage_name: str, items: list[ProductionItem], employee: Employee, skip_worker_check: bool = False) -> int:
        if cls._uses_shared_stage_activities(items):
            return cls.start_group_activity(
                stage_name=stage_name, items=items, employee=employee, skip_worker_check=skip_worker_check
            )
        return cls._start_exclusive_activity(
            stage_name=stage_name, items=items, employee=employee
        )

    @classmethod
    def start_group_activity(
        cls, *, stage_name: str, items: list[ProductionItem], employee: Employee, skip_worker_check: bool = False
    ) -> int:
        """Multiple workers may join the same stage while it is in progress."""
        stage_name = unicodedata.normalize("NFC", stage_name)
        if employee is None:
            raise ValidationError("No employee provided to start activity.")

        # Rule: worker cannot already be active on something else
        if not skip_worker_check:
            cls._ensure_worker_is_free(employee)

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
                stage_info.pop("pause_reason", None)
                cls._save_planning(item, stage_name, stage_info)
            elif stage_info.pop("pause_reason", None) is not None:
                # Resuming a paused stage clears its pause reason.
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
            stage_info.pop("pause_reason", None)
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
    def _finish_open_session(cls, session: ProductionItemSession, completed_by: Employee) -> None:
        if session.kind == ProductionItemSessionKind.ACTIVITY_STARTED:
            session.kind = ProductionItemSessionKind.ACTIVITY_FINISHED
        elif session.kind == ProductionItemSessionKind.SESSION_STARTED:
            session.kind = ProductionItemSessionKind.SESSION_FINISHED
        session.finish(completed_by=completed_by)

    @classmethod
    def supervisor_finish_activity(
        cls,
        *,
        stage_name: str,
        item: ProductionItem,
        attributions: list[tuple[Employee, int]],
    ) -> int:
        """Close a stage on behalf of supervisors, recording who worked and how long."""
        stage_name = unicodedata.normalize("NFC", stage_name)
        now = timezone.now()

        if not attributions:
            raise ValidationError(_("Debe indicar al menos un empleado con tiempo trabajado."))

        seen_ids: set[int] = set()
        for employee, worked_seconds in attributions:
            if employee.id in seen_ids:
                raise ValidationError(_("Cada empleado solo puede aparecer una vez."))
            seen_ids.add(employee.id)
            if worked_seconds < 60:
                raise ValidationError(_("El tiempo mínimo es 1 minuto por empleado."))
            if not employee.can_work_stage(stage_name):
                raise ValidationError(
                    _("%(name)s no puede trabajar en esta etapa.")
                    % {"name": employee.full_name}
                )

        current_stage, stage_info = cls._get_current_stage(item, requested_stage=stage_name)
        if current_stage != stage_name or stage_info is None:
            raise ValidationError(_("Etapa no encontrada en este artículo."))

        status = stage_info.get("status") or ProductionItemStatus.PENDING
        if status in (ProductionItemStatus.DONE, ProductionItemStatus.CANCELLED):
            raise ValidationError(_("La etapa ya está cerrada."))

        attributed_ids = seen_ids

        for sess in cls._open_sessions_for_stage(item, stage_name):
            emp = sess.created_by
            if emp and emp.id not in attributed_ids:
                cls._finish_open_session(sess, completed_by=emp)

        for employee, worked_seconds in attributions:
            open_sess = (
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
            if open_sess:
                open_sess.kind = ProductionItemSessionKind.ACTIVITY_FINISHED
                open_sess.finish_with_duration(
                    completed_by=employee,
                    worked_seconds=worked_seconds,
                )
            else:
                end = now
                start = now - timedelta(seconds=worked_seconds)
                ProductionItemSession.objects.create(
                    production_item=item,
                    production_stage=stage_name,
                    status=ProductionItemSessionStatus.DONE,
                    start_time=start,
                    end_time=end,
                    created_by=employee,
                    completed_by=employee,
                    kind=ProductionItemSessionKind.ACTIVITY_FINISHED,
                )

        for sess in cls._open_sessions_for_stage(item, stage_name):
            emp = sess.created_by
            if emp:
                cls._finish_open_session(sess, completed_by=emp)

        stage_info["status"] = ProductionItemStatus.DONE
        stage_info["started_at"] = stage_info.get("started_at") or now.isoformat()
        stage_info["completed_at"] = now.isoformat()
        stage_info.pop("completed_by", None)
        cls._save_planning(item, stage_name, stage_info)
        return 1

    @classmethod
    def finish_activity(
        cls,
        *,
        stage_name: str,
        items: list[ProductionItem],
        employee: Employee,
        allow_pending: bool = False,
    ) -> int:
        """Mark a stage as done, closing this employee's open sessions.

        With ``allow_pending=True`` a stage that never started can also be
        closed directly (used by the terminal to close leftover tasks on
        fully-invoiced expedientes).
        """
        stage_name = unicodedata.normalize("NFC", stage_name)
        now = timezone.now()
        count = 0

        allowed_statuses = {ProductionItemStatus.IN_PROGRESS}
        if allow_pending:
            allowed_statuses.add(ProductionItemStatus.PENDING)

        for item in items:
            current_stage, stage_info = cls._get_current_stage(item, requested_stage=stage_name)
            if current_stage != stage_name:
                continue

            status = (stage_info.get("status") or ProductionItemStatus.PENDING)
            if status not in allowed_statuses:
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
            stage_info["completed_by"] = employee.pk
            stage_info.pop("pause_reason", None)
            cls._save_planning(item, stage_name, stage_info)
            count += 1
        return count

    @classmethod
    def start_session(cls, *, stage_name: str, items: list[ProductionItem], employee: Employee) -> int:
        stage_name = unicodedata.normalize("NFC", stage_name)
        now = timezone.now()
        count = 0

        cls._ensure_worker_is_free(employee)

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

            if stage_info.pop("pause_reason", None) is not None:
                # Resuming a paused stage clears its pause reason.
                cls._save_planning(item, stage_name, stage_info)

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
    def leave_activity(
        cls,
        *,
        stage_name: str,
        items: list[ProductionItem],
        employee: Employee,
        pause_reason: str | None = None,
    ) -> int:
        """Close this worker's open session without completing the stage for everyone.

        When the stage is left with no open sessions (truly paused) an optional
        ``pause_reason`` from Company.pause_reasons is recorded on the planning.
        """
        stage_name = unicodedata.normalize("NFC", stage_name)
        reason = cls._validated_pause_reason(items, pause_reason)
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

            if reason and not cls._stage_has_open_session(item, stage_name):
                cls._set_pause_reason(item, stage_name, reason)

        if count == 0:
            raise ValidationError(_("No tienes una sesión activa en esta etapa."))
        return count

    @classmethod
    def end_session(
        cls,
        *,
        stage_name: str,
        items: list[ProductionItem],
        employee: Employee,
        pause_reason: str | None = None,
    ) -> int:
        stage_name = unicodedata.normalize("NFC", stage_name)
        reason = cls._validated_pause_reason(items, pause_reason)
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

            if reason and not cls._stage_has_open_session(item, stage_name):
                cls._set_pause_reason(item, stage_name, reason)
        return count

    @classmethod
    def revert_activity(
        cls,
        *,
        stage_name: str,
        items: list[ProductionItem],
        target_status: str = ProductionItemStatus.IN_PROGRESS,
        user=None,
        employee: Employee | None = None,
        action: str = ProductionActions.REOPEN_STAGE,
    ) -> int:
        """Revert a completed stage back to in_progress or pending.

        target_status must be 'in_progress' or 'pending'.
        Open ProductionItemSession rows for this stage are closed (cancelled) so
        planning and sessions stay aligned and a new start_activity can run.
        Finished sessions from prior completions remain in the audit trail, and
        each revert is logged in the stage planning with the acting user/employee.
        Raises ValidationError when a later stage of the item has already started.
        """
        if target_status not in (ProductionItemStatus.IN_PROGRESS, ProductionItemStatus.PENDING):
            raise ValidationError(
                f"Invalid target_status '{target_status}'. Must be 'in_progress' or 'pending'."
            )

        stage_name = unicodedata.normalize("NFC", stage_name)
        now = timezone.now()
        count = 0

        # Which source statuses are valid depends on the target:
        # → pending: accept in_progress or done (can always go back fully)
        # → in_progress: only accept done (undo a finish)
        valid_sources = (
            {ProductionItemStatus.IN_PROGRESS, ProductionItemStatus.DONE}
            if target_status == ProductionItemStatus.PENDING
            else {ProductionItemStatus.DONE}
        )

        stage_orders = {}
        with transaction.atomic():
            locked_items = (
                ProductionItem.objects.select_for_update(of=("self",))
                .select_related("ticket")
                .filter(pk__in=[item.pk for item in items])
                .order_by("pk")
            )
            for item in locked_items:
                planning = {unicodedata.normalize("NFC", k): v for k, v in (item.planning or {}).items()}
                if stage_name not in planning:
                    continue

                stage_info = planning.get(stage_name) or {}
                current_status = stage_info.get("status") or ProductionItemStatus.PENDING
                if current_status not in valid_sources:
                    continue

                if item.ticket_id not in stage_orders:
                    stage_orders[item.ticket_id] = [
                        unicodedata.normalize("NFC", s) for s in item.ticket.get_stage_order()
                    ]
                if item.later_stage_started(stage_name, stage_orders[item.ticket_id]):
                    raise ValidationError(
                        _("A later stage of this item has already started. Reopen that stage first.")
                    )

                cls._close_open_sessions_for_stage(item, stage_name)

                stage_info["status"] = target_status
                stage_info["completed_at"] = None
                stage_info.pop("completed_by", None)
                stage_info.pop("pause_reason", None)
                # Reverting to pending also clears the start timestamp
                if target_status == ProductionItemStatus.PENDING:
                    stage_info["started_at"] = None
                entry = {
                    "at": now.isoformat(),
                    "action": action,
                    "from_status": current_status,
                    "to_status": target_status,
                }
                if user is not None:
                    entry["user_id"] = user.pk
                    entry["user"] = user.get_full_name() or user.get_username()
                if employee is not None:
                    entry["employee_id"] = employee.pk
                    entry["employee"] = employee.full_name
                stage_info.setdefault("revert_log", []).append(entry)
                cls._save_planning(item, stage_name, stage_info)
                count += 1

        return count

    @classmethod
    def undo_finish(cls, *, stage_name: str, items: list[ProductionItem], employee: Employee) -> int:
        """Let a worker undo their own finish within the company's undo window."""
        stage_name = unicodedata.normalize("NFC", stage_name)
        eligible = [item for item in items if item.can_undo_stage_finish(stage_name, employee)]
        if not eligible:
            raise ValidationError(
                _("This stage can no longer be undone. Ask a production supervisor to reopen it.")
            )
        return cls.revert_activity(
            stage_name=stage_name,
            items=eligible,
            target_status=ProductionItemStatus.IN_PROGRESS,
            employee=employee,
            action=ProductionActions.UNDO_FINISH,
        )

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