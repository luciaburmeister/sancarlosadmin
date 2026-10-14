"""
Celery tasks for the export template editor.

The live preview renders a real WeasyPrint PDF, which for a heavy template can
take tens of seconds. Rendering it inside the web request held a gunicorn
thread and a pooled DB connection for the whole render, and an editor typing
fires a render every pause: in October 2026 that exhausted the web pool and
took the site down. The render now runs on the "pdf" queue and the editor
polls for the result.
"""

import base64
import logging

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Results hold a base64 PDF, so they live in the cache (Valkey), never in the
# django-db result backend. Long enough for a slow poller, short enough that
# abandoned previews don't pile up.
PREVIEW_CACHE_TTL = 600


def preview_result_key(token: str) -> str:
    return f"export_preview:{token}"


def preview_latest_key(user_id: int) -> str:
    return f"export_preview:latest:{user_id}"


def _store(token: str, user_id: int, status: str, **data):
    cache.set(
        preview_result_key(token),
        {"user_id": user_id, "status": status, **data},
        PREVIEW_CACHE_TTL,
    )


@shared_task(
    name="configurator.tasks.render_export_template_preview",
    ignore_result=True,
    # Celery kills the child at the hard limit, so one pathological template
    # can't hold a pdf-queue slot (shared with outgoing document emails) for
    # more than this, and a stack overflow kills a child, not a web worker.
    soft_time_limit=30,
    time_limit=40,
)
def render_export_template_preview(token: str, user_id: int, params: dict):
    """
    Render one live preview and store the outcome under `token`.

    `params` is what the editor posted: html, css, kind, product, company,
    object_id. Only the user's latest request is rendered: one that was
    replaced while it sat in the queue stores "superseded" and returns.
    """
    from configurator.models import ConfigurationExportTemplate
    from configurator.services.export import ConfigurationExportService
    from configurator.services.export.kind_registry import get_preview_object
    from core.services.pdf_service import PDFService

    if cache.get(preview_latest_key(user_id)) != token:
        _store(token, user_id, "superseded")
        return

    try:
        target_obj = get_preview_object(
            params.get("kind"),
            product_id=params.get("product"),
            company_id=params.get("company"),
            object_id=params.get("object_id"),
        )
        if not target_obj:
            _store(token, user_id, "error", error="No suitable object available for preview.", http_status=400)
            return

        template = ConfigurationExportTemplate(
            html=params.get("html", ""),
            css=params.get("css", ""),
            kind=params.get("kind"),
        )

        try:
            rendered_body, clean_css, serialized_context = ConfigurationExportService.render_parts(
                target_obj, template
            )
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:
            # Template syntax errors are the common case while someone is typing;
            # surface them in the editor instead of a blank frame.
            logger.info("Export template preview failed to render: %s", exc)
            _store(token, user_id, "error", error=f"Template error: {exc}", http_status=400)
            return

        rendered_html = ConfigurationExportService.wrap_parts(rendered_body, clean_css, serialized_context)

        try:
            pdf_bytes = PDFService.html_to_pdf(rendered_html)
        except RuntimeError as exc:
            _store(token, user_id, "error", error=str(exc), http_status=503)
            return

        _store(
            token, user_id, "done",
            pdf=base64.b64encode(pdf_bytes).decode("ascii"),
            context=serialized_context,
        )
    except SoftTimeLimitExceeded:
        logger.warning("Export template preview %s exceeded its time limit", token)
        _store(
            token, user_id, "error",
            error="The preview took longer than 30 seconds to render. Simplify the template and try again.",
            http_status=504,
        )
    except Exception as exc:
        logger.exception("Export template preview PDF rendering failed")
        _store(token, user_id, "error", error=f"PDF rendering error: {exc}", http_status=500)
