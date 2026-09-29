"""Unfold ``TABS`` entry point: every app's changelist tab bars.

Unfold takes a single callable, so each app keeps its own tabs and they are
listed here.
"""
from django.http import HttpRequest


def admin_tabs(request: HttpRequest) -> list[dict]:
    from assets.admin.tabs import asset_planning_tabs
    from sales.admin.tabs import delivery_order_tabs

    return [*asset_planning_tabs(request), *delivery_order_tabs(request)]
