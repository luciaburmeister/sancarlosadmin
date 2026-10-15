"""
Documents a user downloads from the admin, rendered off the web worker.

A WeasyPrint render (or a heavy export-template HTML render) used to run
inside the request: it held a gunicorn thread and a pooled DB connection for
its whole duration, and a few of them at once took the site down (October
2026). Every such download now goes through here:

1. The view checks permissions and resolves its inputs as before, then calls
   ``enqueue_document()`` with a *renderer* and its keyword arguments.
2. ``core.tasks.render_document_task`` runs the renderer on the "pdf" Celery
   queue, stores the bytes under ``tmp/documents/`` in the default storage and
   returns where they are.
3. The user lands on the status page (core.views.document_status), which
   polls the task and then navigates to the download URL, which serves the
   stored file once and deletes it.

A renderer is a module-level function decorated with ``@document_renderer``.
The task imports it by dotted path, so only functions explicitly marked this
way can be run from a queued message. It takes JSON-serialisable keyword
arguments (ids, not instances) and returns ``bytes`` or ``str`` (the document)
or ``None`` (nothing to render: no template, for example).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.utils.module_loading import import_string

STORAGE_PREFIX = "tmp/documents/"

RENDERER_MARKER = "_is_document_renderer"


def document_renderer(func):
    """Mark ``func`` as safe to run from ``render_document_task``."""
    setattr(func, RENDERER_MARKER, True)
    return func


def renderer_path(func) -> str:
    if not getattr(func, RENDERER_MARKER, False):
        raise TypeError(f"{func!r} is not decorated with @document_renderer")
    return f"{func.__module__}.{func.__qualname__}"


def load_renderer(path: str):
    func = import_string(path)
    if not getattr(func, RENDERER_MARKER, False):
        raise ValueError(f"{path} is not a registered document renderer")
    return func


def safe_filename(value: str, fallback: str) -> str:
    raw = (value or "").strip() or fallback
    cleaned = "".join(c if c.isalnum() or c in "-_." else "_" for c in raw)
    return cleaned or fallback


@dataclass
class DocumentRequest:
    """What ``render_document_task`` needs to produce one download."""

    renderer: str
    kwargs: dict
    filename: str
    content_type: str = "application/pdf"
    # True opens the document in the tab (PDF viewer / HTML page) instead of
    # saving it: what the sync views used for exports opened in a new tab.
    inline: bool = False
    user_id: int | None = None

    def as_task_kwargs(self) -> dict:
        return asdict(self)


def enqueue_document(
    request,
    renderer,
    *,
    filename: str,
    back_url: str,
    content_type: str = "application/pdf",
    inline: bool = False,
    **kwargs,
):
    """
    Queue ``renderer(**kwargs)`` on the pdf queue and redirect the user to the
    status page, which downloads the result when it is ready.

    ``back_url`` is where the status page's "Go back" link leads (and where
    the user returns after an error). The caller has already checked the
    user may see this document: nothing here re-checks it.
    """
    from core.tasks import render_document_task

    document = DocumentRequest(
        renderer=renderer_path(renderer),
        kwargs=kwargs,
        filename=filename,
        content_type=content_type,
        inline=inline,
        user_id=request.user.pk,
    )
    result = render_document_task.delay(**document.as_task_kwargs())
    return redirect_to_document_status(result.id, back_url)


def redirect_to_document_status(task_id: str, back_url: str):
    if not back_url or not url_has_allowed_host_and_scheme(back_url, allowed_hosts=None):
        back_url = reverse("admin:index")
    return redirect(
        reverse("core_document_status") + "?" + urlencode({"task_id": task_id, "back_url": back_url})
    )
