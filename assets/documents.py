"""The public asset transaction document (the albarán behind the QR code),
and the per-DI variant of it for a split AssetTransactionReceipt -- same
template and rendering pipeline, with that one DI's own nro_albaran/material/
peso/volumen substituted in for the transaction's."""
import logging

from celery.result import AsyncResult
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.utils import timezone

from assets.models import AssetTransaction, AssetTransactionPause, AssetTransactionReceipt
from configurator.models import (
    ConfigurationExportTemplate,
    ExportTemplateKind,
    ExportTemplateSubtype,
)
from configurator.services.export.service import ConfigurationExportService
from configurator.templatetags.formatting import qr
from core.services.pdf_service import PDFService

logger = logging.getLogger(__name__)

# Longer than the task's time limit: while a render is queued or running,
# further visits wait on it instead of queueing another.
GENERATION_LOCK_SECONDS = 300


def document_filename(transaction) -> str:
    return f"transaction_{transaction.pk}.pdf"


def receipt_document_filename(receipt) -> str:
    return f"transaction_receipt_{receipt.pk}.pdf"


def pause_document_filename(pause) -> str:
    return f"transaction_pause_{pause.pk}.pdf"


def get_document_template(transaction):
    return ConfigurationExportTemplate.get_default_for_product(
        product=None,
        kind=ExportTemplateKind.ASSET_TRANSACTION,
        company=transaction.company,
        subtype=ExportTemplateSubtype.DOCUMENT,
    )


def _generation_cache_key(transaction_id: int) -> str:
    return f"asset-transaction-document:{transaction_id}"


def _receipt_generation_cache_key(receipt_id: int) -> str:
    return f"asset-transaction-receipt-document:{receipt_id}"


def _pause_generation_cache_key(pause_id: int) -> str:
    return f"asset-transaction-pause-document:{pause_id}"


def _queue(*, cache_key: str, task) -> str:
    """Shared "share an in-flight render" logic for queue_document_generation/
    queue_receipt_generation: returns the task id, queueing a new render
    only if nothing is already queued/running for this cache key."""
    try:
        task_id = cache.get(cache_key)
    except Exception:
        logger.warning("Cache unavailable; queueing document render (key=%s)", cache_key, exc_info=True)
        task_id = None
    if task_id and not AsyncResult(task_id).failed():
        return task_id

    task_id = task().id
    try:
        cache.set(cache_key, task_id, GENERATION_LOCK_SECONDS)
    except Exception:
        logger.warning("Cache unavailable; document task (key=%s) not shared", cache_key, exc_info=True)
    return task_id


def queue_document_generation(transaction_id: int) -> str:
    """
    Queue the first render of a transaction's document and return the task
    id; visits while that render is queued or running share it.
    """
    from assets.tasks import generate_asset_transaction_document

    return _queue(
        cache_key=_generation_cache_key(transaction_id),
        task=lambda: generate_asset_transaction_document.delay(transaction_id),
    )


def queue_receipt_generation(receipt_id: int) -> str:
    """Same as queue_document_generation, for one DI (AssetTransactionReceipt)."""
    from assets.tasks import generate_asset_transaction_receipt_document

    return _queue(
        cache_key=_receipt_generation_cache_key(receipt_id),
        task=lambda: generate_asset_transaction_receipt_document.delay(receipt_id),
    )


def queue_pause_generation(pause_id: int) -> str:
    """Same as queue_document_generation, for one AssetTransactionPause."""
    from assets.tasks import generate_asset_transaction_pause_document

    return _queue(
        cache_key=_pause_generation_cache_key(pause_id),
        task=lambda: generate_asset_transaction_pause_document.delay(pause_id),
    )


def schedule_regeneration(transaction_id: int) -> None:
    """Re-render the stored document once the current save commits."""
    from django.db import transaction as db_transaction

    from assets.tasks import generate_asset_transaction_document

    db_transaction.on_commit(lambda: generate_asset_transaction_document.delay(transaction_id))


def schedule_receipt_regeneration(receipt_id: int) -> None:
    """Same as schedule_regeneration, for one DI (AssetTransactionReceipt)."""
    from django.db import transaction as db_transaction

    from assets.tasks import generate_asset_transaction_receipt_document

    db_transaction.on_commit(lambda: generate_asset_transaction_receipt_document.delay(receipt_id))


def schedule_pause_regeneration(pause_id: int) -> None:
    """Same as schedule_regeneration, for one AssetTransactionPause."""
    from django.db import transaction as db_transaction

    from assets.tasks import generate_asset_transaction_pause_document

    db_transaction.on_commit(lambda: generate_asset_transaction_pause_document.delay(pause_id))


def _render_document_pdf(
    transaction, *, document_url: str, document_number: int, data_override: dict | None = None,
    context_override: dict | None = None,
) -> bytes:
    """Renders the company's ASSET_TRANSACTION/DOCUMENT template for
    `transaction`, laying `data_override` (one DI's own nro_albaran/material/
    peso, when rendering for a split receipt) on top of the transaction's own
    `data`. `document_number` is the printed "Nº" -- passed in rather than
    read from `transaction.id` in the template, since a split receipt's
    document needs its own DI number, not the (shared) parent transaction's.
    `context_override` replaces top-level context keys instead (e.g. `date`,
    for a pause's document -- see generate_pause_document). Returns the PDF
    bytes."""
    data = {
        **(transaction.data or {}),
        "reference_number": transaction.reference_number or (transaction.data or {}).get("reference_number", ""),
    }
    if data_override:
        data.update(data_override)
    extra_context = {
        "data": data,
        "document_url": document_url,
        "document_qr": qr(document_url),
        "document_number": document_number,
    }
    if context_override:
        extra_context.update(context_override)
    html = ConfigurationExportService.render_html(
        transaction, get_document_template(transaction), extra_context=extra_context,
    )
    return PDFService.html_to_pdf(html)


def _store_rendered_document(obj, manager, pdf_bytes: bytes, filename: str) -> None:
    """Shared "save the file, then only keep it if the row hasn't moved on
    since we read it" logic for generate_document/generate_receipt_document."""
    field = obj.document_file
    name = field.storage.save(
        field.field.generate_filename(obj, filename), ContentFile(pdf_bytes),
    )
    stored = manager.filter(
        pk=obj.pk, updated_at=obj.updated_at, document_file=field.name or "",
    ).update(document_file=name, document_generated_at=timezone.now())
    if not stored:
        field.storage.delete(name)


def generate_document(transaction_id: int, force: bool = False) -> bool:
    """
    Render and store the transaction's document if it has none, or if it
    changed since the stored one was rendered (always, with ``force``: a
    template edit, for example). Returns whether the transaction has a
    stored document afterwards (False: no template).

    The render is only stored if the transaction is unchanged since it was
    read, so a slow render of an older version never replaces a newer one
    (the save that changed it queued its own render). Superseded files stay
    in storage; only the field moves to the new one.
    """
    transaction = AssetTransaction.objects.select_related(
        "asset", "asset__asset_type", "asset__client",
        "company", "project_file", "project_file__client",
        "from_zone", "to_zone", "performed_by",
    ).get(pk=transaction_id)
    generated_at = transaction.document_generated_at
    if not force and transaction.document_file and generated_at and generated_at >= transaction.updated_at:
        return True
    if get_document_template(transaction) is None:
        return bool(transaction.document_file)

    pdf_bytes = _render_document_pdf(
        transaction, document_url=transaction.document_url,
        document_number=transaction.di_number or transaction.id,
    )
    _store_rendered_document(
        transaction, AssetTransaction.objects, pdf_bytes, document_filename(transaction),
    )
    return True


def generate_receipt_document(receipt_id: int, force: bool = False) -> bool:
    """Same as generate_document, for one DI (AssetTransactionReceipt) -- the
    template is the transaction's own, but nro_albaran/material_vertido/
    peso_neto/m3/part_number/total_parts come from this receipt instead, so
    each part of a split DI gets its own correct document."""
    receipt = AssetTransactionReceipt.objects.select_related(
        "transaction__asset", "transaction__asset__asset_type", "transaction__asset__client",
        "transaction__company", "transaction__project_file", "transaction__project_file__client",
        "transaction__from_zone", "transaction__to_zone", "transaction__performed_by",
    ).get(pk=receipt_id)
    generated_at = receipt.document_generated_at
    if not force and receipt.document_file and generated_at and generated_at >= receipt.updated_at:
        return True
    transaction = receipt.transaction
    if get_document_template(transaction) is None:
        return bool(receipt.document_file)

    data_override = {
        "nro_albaran": receipt.nro_albaran,
        "part_number": receipt.part_number,
        "total_parts": receipt.total_parts,
        **(receipt.data or {}),
    }
    pdf_bytes = _render_document_pdf(
        transaction, document_url=receipt.document_url, data_override=data_override,
        document_number=receipt.di_number or receipt.transaction_id,
    )
    _store_rendered_document(
        receipt, AssetTransactionReceipt.objects, pdf_bytes, receipt_document_filename(receipt),
    )
    return True


def generate_pause_document(pause_id: int, force: bool = False) -> bool:
    """Same as generate_document, for one AssetTransactionPause -- the
    template and base data are the pickup movement's own, with
    is_pause/original_di_number/original_pickup_date laid on top so the
    template can note that this leg doesn't start from the documented
    origin, but continues an earlier pickup instead."""
    pause = AssetTransactionPause.objects.select_related(
        "movement__asset", "movement__asset__asset_type", "movement__asset__client",
        "movement__company", "movement__project_file", "movement__project_file__client",
        "movement__from_zone", "movement__to_zone", "movement__performed_by",
    ).get(pk=pause_id)
    generated_at = pause.document_generated_at
    if not force and pause.document_file and generated_at and generated_at >= pause.updated_at:
        return True
    movement = pause.movement
    if get_document_template(movement) is None:
        return bool(pause.document_file)

    data_override = {
        "is_pause": True,
        "original_di_number": movement.di_number,
        "original_pickup_date": movement.date,
    }
    pdf_bytes = _render_document_pdf(
        movement, document_url=pause.document_url, data_override=data_override,
        document_number=pause.di_number, context_override={"date": pause.date},
    )
    _store_rendered_document(
        pause, AssetTransactionPause.objects, pdf_bytes, pause_document_filename(pause),
    )
    return True
