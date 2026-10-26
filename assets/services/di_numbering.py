"""
Shared sequential numbering for DI (Documento de Identificación) documents --
one counter per company, spanning three different models: AssetTransaction
(a full-cuba MOVEMENT or a PORTE_MOVEMENT pickup, see
AssetTransaction.requires_di_number), AssetTransactionReceipt (every
verteo DI, including each part of a split), and AssetTransactionPause (a
movement's second DI, when it sat paused before its verteo). All three draw
from the same sequence, same mechanics as
ProjectFileService.generate_next_reference_data and
SalesInvoiceService._generate_next_invoice_data -- lock the highest-numbered
row and take the next integer, just across three tables instead of one.
"""
from django.db import transaction


class DiNumberingService:

    @staticmethod
    @transaction.atomic
    def allocate_di_number(company) -> int:
        """Reads only `pk`/`di_number` from each table (`.only(...)`), not a
        full row -- this also runs from the 0088 historical migration with
        the live models (see transaction_pause.py), so selecting every
        *current* column of AssetTransaction/Receipt/Pause would break the
        moment a later migration adds one of those models a column this
        migration predates, which is exactly what happened twice already."""
        from assets.models import AssetTransaction, AssetTransactionPause, AssetTransactionReceipt

        last_tx = AssetTransaction.objects.filter(
            company=company, di_number__isnull=False,
        ).only("pk", "di_number").select_for_update().order_by("-di_number").first()
        last_receipt = AssetTransactionReceipt.objects.filter(
            transaction__company=company, di_number__isnull=False,
        ).only("pk", "di_number").select_for_update().order_by("-di_number").first()
        last_pause = AssetTransactionPause.objects.filter(
            movement__company=company, di_number__isnull=False,
        ).only("pk", "di_number").select_for_update().order_by("-di_number").first()
        current_max = max(
            last_tx.di_number if last_tx else 0,
            last_receipt.di_number if last_receipt else 0,
            last_pause.di_number if last_pause else 0,
        )
        return current_max + 1

    @staticmethod
    def validate_di_number_unique(instance, di_number, company) -> None:
        """Called from each DI-bearing model's own clean() -- a hand-edit by
        a superuser (see get_readonly_fields) is the only way di_number ever
        changes after creation, and nothing else guards it against colliding
        with a number that already exists on any of the three tables. A
        no-op when di_number is still None (new rows get theirs from
        allocate_di_number, which already can't collide by construction).

        A MOVEMENT and the first receipt filed against it legitimately share
        one number (see AssetTransactionReceipt._resolve_di_number) -- that
        pairing is excluded from the conflict check, not just self;
        everything else colliding is a genuine mistake and raises
        ValidationError.
        """
        if di_number is None:
            return
        from django.core.exceptions import ValidationError
        from django.utils.translation import gettext_lazy as _

        from assets.models import AssetTransaction, AssetTransactionPause, AssetTransactionReceipt

        def _conflicts(model, lookup):
            qs = model.objects.filter(di_number=di_number, **lookup)
            if isinstance(instance, model):
                qs = qs.exclude(pk=instance.pk)
            return qs

        tx_conflicts = _conflicts(AssetTransaction, {"company": company})
        receipt_conflicts = _conflicts(AssetTransactionReceipt, {"transaction__company": company})
        pause_conflicts = _conflicts(AssetTransactionPause, {"movement__company": company})

        if isinstance(instance, AssetTransaction):
            receipt_conflicts = receipt_conflicts.exclude(movement_transaction_id=instance.pk)
        elif isinstance(instance, AssetTransactionReceipt) and instance.movement_transaction_id:
            tx_conflicts = tx_conflicts.exclude(pk=instance.movement_transaction_id)

        if tx_conflicts.exists() or receipt_conflicts.exists() or pause_conflicts.exists():
            raise ValidationError({
                "di_number": _("Another document already uses DI number %(n)d.") % {"n": di_number},
            })
