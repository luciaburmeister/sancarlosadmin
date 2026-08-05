"""
Truck ↔ cuba compatibility, combining two mechanisms:

1. TransportCompatibilityRule — admin-editable expressions (safe_eval) that
   compare characteristics (AssetType.schema / Asset.data), for compatibility
   that follows a general rule (e.g. weight capacity). ALL active rules for
   the planning's company must pass.
2. Asset.excluded_assets — a direct, manual exclusion between two specific
   assets, for one-off cases that don't fit a clean characteristic-based rule
   (e.g. "this truck's crane is broken, don't use it with this cuba even
   though it technically qualifies").

A truck is compatible only if it clears both.
"""
from __future__ import annotations

from django.utils.translation import gettext as _

from configurator.utils.evaluator import safe_eval

from assets.models import Asset, AssetPlanning, TransportCompatibilityRule


def _asset_context(asset: Asset | None) -> dict:
    if asset is None:
        return {}
    context = dict(asset.data or {})
    context.update({
        "name": asset.name,
        "asset_type": asset.asset_type.name if asset.asset_type_id else "",
        "working_zone": asset.current_working_zone.name if asset.current_working_zone_id else "",
        "is_active": asset.is_active,
    })
    return context


class CompatibilityChecker:
    """
    Checks candidate trucks against one planning's cuba.

    Caches the company's active TransportCompatibilityRule rows once at
    construction, so checking many candidates (e.g. from the assignment
    algorithm, one cuba against every active truck) doesn't re-query them
    per truck — build one checker per planning, then call is_compatible/
    reasons per candidate.
    """

    def __init__(self, planning: AssetPlanning):
        self.planning = planning
        self.cuba = planning.asset
        self.rules = list(
            TransportCompatibilityRule.objects.filter(company_id=planning.company_id, is_active=True)
        )

    def context_for(self, truck: Asset) -> dict:
        """cuba.<field> / truck.<field> namespaces for TransportCompatibilityRule expressions."""
        context = {
            "cuba": _asset_context(self.cuba),
            "truck": _asset_context(truck),
        }
        context["cuba"]["size"] = self.planning.cuba_size
        context["cuba"]["operation_type"] = self.planning.cuba_operation_type
        return context

    def reasons(self, truck: Asset) -> list[str]:
        """Names of every rule this truck fails, plus a manual exclusion if any; empty list = compatible."""
        if self.cuba is None:
            # Nothing to compare (e.g. a porte with no asset) — compatibility
            # doesn't apply, not "everything fails closed".
            return []

        reasons = []
        if self.cuba.excluded_assets.filter(pk=truck.pk).exists():
            reasons.append(str(_("Manually excluded")))

        context = self.context_for(truck)
        for rule in self.rules:
            try:
                passed = bool(safe_eval(rule.condition_expression, context))
            except ValueError:
                # A rule that can't evaluate against this pair (e.g. a data
                # field this cuba/truck doesn't have) blocks it too — fail
                # closed, not open, and never let a bad expression break the
                # assignment flow.
                passed = False
            if not passed:
                reasons.append(rule.name)
        return reasons

    def is_compatible(self, truck: Asset) -> bool:
        return not self.reasons(truck)
