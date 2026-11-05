"""
Records shown by the sidebar search / Cmd+K palette (UNFOLD["COMMAND"]).

Unfold's built-in `search_models` builds a result for every match before
paginating, and orders models alphabetically by app. This runs the models in
the order below and fetches only a few rows per model, newest first, with a
link to the full changelist search when there are more.

Each model goes through its own admin: `get_queryset` keeps the company
scoping, `get_search_results` uses the admin's `search_fields`, and models
the user cannot view are skipped.
"""
from django.apps import apps
from django.contrib import admin
from django.db.models import Subquery
from django.urls import reverse
from django.utils.http import urlencode
from django.utils.translation import gettext as _
from unfold.dataclasses import SearchResult

# (model label, Material Symbols icon), in display order
SEARCH_MODELS = (
    ("sales.Client", "person"),
    ("materials.Provider", "local_shipping"),
    ("management.ProjectFile", "folder"),
    ("sales.Quote", "request_quote"),
    ("invoices.SalesInvoice", "receipt_long"),
)
PER_MODEL = 10


def search_callback(request, search_term):
    results = []
    for label, icon in SEARCH_MODELS:
        model = apps.get_model(label)
        model_admin = admin.site._registry.get(model)
        if model_admin is None or not model_admin.has_view_or_change_permission(request):
            continue
        results.extend(_search_model(request, model_admin, search_term, icon))
    return results


def _search_model(request, model_admin, search_term, icon):
    opts = model_admin.model._meta
    qs = model_admin.get_queryset(request)
    qs, may_have_duplicates = model_admin.get_search_results(request, qs, search_term)
    if may_have_duplicates:
        # Dedupe on pk rather than DISTINCT over the admin's annotations,
        # which would compute them for every match before the LIMIT.
        qs = model_admin.get_queryset(request).filter(pk__in=Subquery(qs.values("pk")))
    if isinstance(model_admin.list_select_related, (list, tuple)):
        qs = qs.select_related(*model_admin.list_select_related)

    rows = list(qs.order_by("-pk")[: PER_MODEL + 1])
    verbose_name = str(opts.verbose_name).capitalize()
    results = [
        SearchResult(
            title=str(obj),
            description=verbose_name,
            link=reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=(obj.pk,)),
            icon=icon,
        )
        for obj in rows[:PER_MODEL]
    ]
    if len(rows) > PER_MODEL:
        changelist = reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist")
        results.append(
            SearchResult(
                title=_("See all results in %(model)s") % {"model": opts.verbose_name_plural},
                description=verbose_name,
                link=f"{changelist}?{urlencode({'q': search_term})}",
                icon="manage_search",
            )
        )
    return results
