from django.db import transaction as db_transaction
from django.db.models import Exists, OuterRef, Prefetch
from django.utils import timezone


class TransactionPauseService:
    """
    Detects a full-cuba MOVEMENT that sat paused across a calendar-day
    boundary before being resolved -- either already dumped (its receipt's
    date differs from the pickup's) or still pending (unresolved as of
    today) -- and gives it a second DI via AssetTransactionPause (see
    assets.models).

    Used both by the daily Celery Beat task
    (assets.tasks.detect_transaction_pauses, going forward) and by the
    one-off historical backfill migration -- same detection, same creation,
    just run once for the past instead of daily.
    """

    @staticmethod
    def _candidate_movements(AssetTransaction, AssetTransactionReceipt, TransactionTypeChoices):
        """Full-cuba movements that already have a di_number (so,
        post-cutoff) and don't have a pause yet, restricted to the ones not
        superseded by a later pickup for the same asset (a drop-off-and-
        pickup cycle already gets its own fresh di_number on that later
        movement, see AssetTransactionReceipt._resolve_di_number -- nothing
        to do here). receipt_links is preloaded so resolving each candidate
        doesn't need its own query. company is deliberately not
        select_related -- AssetTransactionPause.save() only ever needs its
        id (see DiNumberingService), and this is also called from the 0088
        historical migration with the live models, where eagerly joining
        Company's current columns would break against an older schema."""
        later_movement_exists = AssetTransaction.objects.filter(
            asset=OuterRef("asset"), transaction_type=TransactionTypeChoices.MOVEMENT, date__gt=OuterRef("date"),
        )
        return AssetTransaction.objects.filter(
            transaction_type=TransactionTypeChoices.MOVEMENT,
            di_number__isnull=False,
            pause__isnull=True,
        ).annotate(
            has_later_movement=Exists(later_movement_exists),
        ).filter(has_later_movement=False).prefetch_related(
            Prefetch(
                "receipt_links",
                queryset=AssetTransactionReceipt.objects.select_related("transaction").order_by("created_at"),
            ),
        )

    @staticmethod
    def _needs_pause(movement, today) -> bool:
        """True if at least one full calendar day passed between the pickup
        and its resolution -- the date of the receipt filed against it, if
        any, else today, if it's still pending."""
        pickup_date = timezone.localtime(movement.date).date()
        receipt = next(iter(movement.receipt_links.all()), None)
        resolution_date = (
            timezone.localtime(receipt.transaction.date).date() if receipt is not None else today
        )
        return resolution_date != pickup_date

    @classmethod
    def generate_due_pauses(cls, today=None):
        """Creates one AssetTransactionPause for every qualifying movement
        that doesn't already have one. Returns the created rows."""
        from assets.documents import schedule_pause_regeneration
        from assets.models import (
            AssetTransaction,
            AssetTransactionPause,
            AssetTransactionReceipt,
            TransactionTypeChoices,
        )

        today = today or timezone.localdate()
        created = []
        candidates = cls._candidate_movements(AssetTransaction, AssetTransactionReceipt, TransactionTypeChoices)
        for movement in candidates:
            if not cls._needs_pause(movement, today):
                continue
            with db_transaction.atomic():
                pause = AssetTransactionPause(movement=movement)
                pause.save()
            created.append(pause)
            schedule_pause_regeneration(pause.pk)
        return created
