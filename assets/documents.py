"""The public asset transaction document (the albarán behind the QR code)."""
import logging

from celery.result import AsyncResult
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.utils import timezone

from assets.models import AssetTransaction
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


def get_document_template(transaction):
    return ConfigurationExportTemplate.get_default_for_product(
        product=None,
        kind=ExportTemplateKind.ASSET_TRANSACTION,
        company=transaction.company,
        subtype=ExportTemplateSubtype.DOCUMENT,
    )


def _generation_cache_key(transaction_id: int) -> str:
    return f"asset-transaction-document:{transaction_id}"


def queue_generation(transaction_id: int) -> str:
    """
    Queue the first render of a transaction's document and return the task
    id; visits while that render is queued or running share it.
    """
    from assets.tasks import generate_asset_transaction_document

    key = _generation_cache_key(transaction_id)
    try:
        task_id = cache.get(key)
    except Exception:
        logger.warning("Cache unavailable; queueing document for transaction %s", transaction_id, exc_info=True)
        task_id = None
    if task_id and not AsyncResult(task_id).failed():
        return task_id

    task_id = generate_asset_transaction_document.delay(transaction_id).id
    try:
        cache.set(key, task_id, GENERATION_LOCK_SECONDS)
    except Exception:
        logger.warning("Cache unavailable; document task for transaction %s not shared", transaction_id, exc_info=True)
    return task_id


def schedule_regeneration(transaction_id: int) -> None:
    """Re-render the stored document once the current save commits."""
    from django.db import transaction as db_transaction

    from assets.tasks import generate_asset_transaction_document

    db_transaction.on_commit(lambda: generate_asset_transaction_document.delay(transaction_id))


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
    template = get_document_template(transaction)
    if template is None:
        return bool(transaction.document_file)

    document_url = transaction.document_url
    html = ConfigurationExportService.render_html(
        transaction,
        template,
        extra_context={
            "document_url": document_url,
            "document_qr": qr(document_url),
        },
    )
    pdf_bytes = PDFService.html_to_pdf(html)

    field = transaction.document_file
    name = field.storage.save(
        field.field.generate_filename(transaction, document_filename(transaction)), ContentFile(pdf_bytes),
    )
    stored = AssetTransaction.objects.filter(
        pk=transaction.pk, updated_at=transaction.updated_at, document_file=field.name or "",
    ).update(document_file=name, document_generated_at=timezone.now())
    if not stored:
        field.storage.delete(name)
    return True
