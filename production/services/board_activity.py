"""Aggregates real ProductionItemSession activity by (ticket, stage) into
active/paused/done entries for the weekly scheduling board. Sessions are
clustered into contiguous work stints first, so a pause gap always produces
separate, non-overlapping entries instead of one span covering the gap."""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta

from django.contrib.contenttypes.prefetch import GenericPrefetch
from django.db.models import Q, TextChoices
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from api._serializers import _stage_events
from production.models import (
    PlannedProductionSession,
    ProductionItem,
    ProductionItemSession,
    ProductionItemSessionStatus,
    ProductionItemStatus,
    ProductionTicket,
    ProductionTicketStatus,
)
from production.services.ticket_labels import build_ticket_labels


class BoardActivityKind(TextChoices):
    """How one clustered work stint classifies on the board."""

    ACTIVE = "active", _("Active")
    PAUSED = "paused", _("Paused")
    DONE = "done", _("Done")


class WeekActivityBuilder:
    """Builds one board entry per (ticket, stage, work stint) with live or
    recently-finished ProductionItemSession activity, for one company and
    week window."""

    RECENCY_WINDOW = timedelta(days=14)

    def __init__(self, company, range_start: datetime, range_end: datetime):
        self.company = company
        self.range_start = range_start
        self.range_end = range_end
        self.range_start_date = range_start.date()

    def build(self) -> list[dict]:
        items = list(self._relevant_items())
        item_ids = self._items_with_live_or_done_planning(items)
        if not item_ids:
            return []

        sessions = self._load_sessions(item_ids)
        if not sessions:
            return []

        groups = self._group_sessions(sessions)
        ticket_ids = {ticket_id for ticket_id, _stage in groups}
        tickets = self._load_tickets(ticket_ids)
        planned_index = self._load_planned_sessions(ticket_ids)

        entries: list[dict] = []
        for (ticket_id, stage), group_sessions in groups.items():
            entries.extend(
                self._build_entries(ticket_id, stage, group_sessions, tickets, planned_index)
            )
        return entries

    def _relevant_items(self):
        """Items with live (any age) or recently-done activity — not scoped by
        session date, so a long-paused stage still shows."""
        recency_floor = self.range_start - self.RECENCY_WINDOW
        return (
            ProductionItem.objects.filter(company=self.company)
            .exclude(ticket__status=ProductionTicketStatus.CANCELLED)
            .filter(
                Q(ticket__status__in=[ProductionTicketStatus.PENDING, ProductionTicketStatus.IN_PROGRESS])
                | Q(ticket__status=ProductionTicketStatus.DONE, ticket__updated_at__gte=recency_floor)
            )
            .select_related("ticket")
        )

    @staticmethod
    def _items_with_live_or_done_planning(items) -> set[int]:
        item_ids: set[int] = set()
        for item in items:
            for info in (item.planning or {}).values():
                status = (info or {}).get("status")
                if status in (ProductionItemStatus.IN_PROGRESS, ProductionItemStatus.DONE):
                    item_ids.add(item.id)
                    break
        return item_ids

    @staticmethod
    def _load_sessions(item_ids: set[int]) -> list[ProductionItemSession]:
        return list(
            ProductionItemSession.objects.filter(
                production_item_id__in=item_ids, start_time__isnull=False,
            )
            .exclude(status=ProductionItemSessionStatus.CANCELLED)
            .select_related("created_by", "production_item", "production_item__ticket")
            .order_by("production_item__ticket_id", "production_stage", "start_time")
        )

    @staticmethod
    def _group_sessions(sessions) -> dict[tuple[int, str], list]:
        groups: dict[tuple[int, str], list] = {}
        for sess in sessions:
            key = (
                sess.production_item.ticket_id,
                unicodedata.normalize("NFC", sess.production_stage),
            )
            groups.setdefault(key, []).append(sess)
        return groups

    @staticmethod
    def _load_planned_sessions(ticket_ids: set[int]) -> dict[tuple[int, str], PlannedProductionSession]:
        """(ticket_id, normalized stage) -> its current plan, if any — lets an
        activity entry compare real progress against the original estimate
        (see `is_late`/`planned_session_id` on the built entry). A stage is
        only ever planned into one slot at a time, so the latest by
        planned_start wins if, unexpectedly, more than one is found."""
        index: dict[tuple[int, str], PlannedProductionSession] = {}
        sessions = PlannedProductionSession.objects.filter(
            production_ticket_id__in=ticket_ids
        ).order_by("planned_start")
        for session in sessions:
            key = (session.production_ticket_id, unicodedata.normalize("NFC", session.production_stage))
            index[key] = session
        return index

    @staticmethod
    def _load_tickets(ticket_ids: set[int]) -> dict[int, ProductionTicket]:
        from configurator.models import Configuration

        return {
            t.id: t
            for t in ProductionTicket.objects.filter(id__in=ticket_ids)
            .select_related("project_file__client")
            .prefetch_related(
                GenericPrefetch(
                    "source_object", [Configuration.objects.select_related("product", "quote")]
                )
            )
        }

    @staticmethod
    def _cluster_sessions(sessions: list) -> list[list]:
        ordered = sorted(sessions, key=lambda s: s.start_time)
        clusters: list[list] = [[ordered[0]]]
        cluster_end = ordered[0].end_time
        for sess in ordered[1:]:
            if cluster_end is None or sess.start_time <= cluster_end:
                clusters[-1].append(sess)
                if cluster_end is not None:
                    cluster_end = None if sess.end_time is None else max(cluster_end, sess.end_time)
            else:
                clusters.append([sess])
                cluster_end = sess.end_time
        return clusters

    def _build_entries(
        self, ticket_id: int, stage: str, group_sessions: list, tickets: dict, planned_index: dict,
    ) -> list[dict]:
        item_statuses: dict[int, str] = {}
        for sess in group_sessions:
            item = sess.production_item
            raw = item.get_stage_planning(stage)
            item_statuses[item.id] = raw.get("status") or ProductionItemStatus.PENDING

        all_done = bool(item_statuses) and all(
            status == ProductionItemStatus.DONE for status in item_statuses.values()
        )
        if all_done:
            last_end = max((s.end_time for s in group_sessions if s.end_time), default=None)
            if last_end is None or not (self.range_start <= last_end < self.range_end):
                return []

        ticket = tickets.get(ticket_id)
        if ticket is None:
            return []
        labels = build_ticket_labels(
            ticket=ticket, config=ticket.source_object, project_file=ticket.resolved_project_file,
        )

        stage_key = (ticket_id, unicodedata.normalize("NFC", stage))
        planned_session = planned_index.get(stage_key)

        return [
            self._build_cluster_entry(
                cluster, item_statuses, all_done, ticket_id, stage, labels, planned_session,
            )
            for cluster in self._cluster_sessions(group_sessions)
        ]

    def _build_cluster_entry(
        self, cluster: list, item_statuses: dict, all_done: bool, ticket_id: int, stage: str, labels,
        planned_session,
    ) -> dict:
        open_sessions = [s for s in cluster if s.end_time is None]

        if all_done:
            kind = BoardActivityKind.DONE
        elif any(
            item_statuses.get(s.production_item_id) == ProductionItemStatus.IN_PROGRESS
            for s in open_sessions
        ):
            kind = BoardActivityKind.ACTIVE
        else:
            kind = BoardActivityKind.PAUSED

        start_at = min(s.start_time for s in cluster)
        closed_ends = [s.end_time for s in cluster if s.end_time]
        end_at = max(closed_ends) if closed_ends and not open_sessions else None
        raw_date = (end_at or start_at).date()
        date = raw_date if kind == BoardActivityKind.DONE else max(raw_date, self.range_start_date)

        employee = cluster[-1].created_by

        active_worker_ids: set[int] = set()
        active_workers: list[dict] = []
        for sess in open_sessions:
            if sess.created_by and sess.created_by.id not in active_worker_ids:
                active_worker_ids.add(sess.created_by.id)
                active_workers.append({"id": sess.created_by.id, "full_name": sess.created_by.full_name})

        comments: list[dict] = []
        for sess in cluster:
            emp = sess.created_by
            if emp is None:
                continue
            for entry in sess.comment_entries or []:
                if not isinstance(entry, dict):
                    continue
                text = (entry.get("text") or "").strip()
                if text:
                    comments.append({"employee_name": emp.full_name, "comment": text, "at": entry.get("at")})

        item_ids = sorted({s.production_item_id for s in cluster})

        is_late = (
            planned_session is not None
            and kind != BoardActivityKind.DONE
            and planned_session.planned_end < timezone.now()
        )

        return {
            "kind": kind,
            "production_ticket_id": ticket_id,
            "production_stage": stage,
            "production_item_ids": item_ids,
            "date": date.isoformat(),
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat() if end_at else None,
            "employee_id": employee.id if employee else None,
            "employee_name": employee.full_name if employee else None,
            "active_workers": active_workers,
            "session_comments": comments,
            "session_events": _stage_events(cluster, kind),
            "planned_session_id": planned_session.id if planned_session else None,
            "planned_start": planned_session.planned_start.isoformat() if planned_session else None,
            "planned_end": planned_session.planned_end.isoformat() if planned_session else None,
            "is_late": is_late,
            "config_id": labels.config_id,
            "config_name": labels.config_name,
            "config_notes": labels.config_notes,
            "quote_name": labels.quote_name,
            "template_name": labels.template_name,
            "ticket_status": labels.ticket_status,
            "config_created_at": labels.config_created_at,
            "expediente": labels.expediente,
        }


def build_week_activity(company, range_start: datetime, range_end: datetime) -> list[dict]:
    """One entry per (ticket, stage, work stint) currently in_progress, or
    finished within [range_start, range_end)."""
    return WeekActivityBuilder(company, range_start, range_end).build()
