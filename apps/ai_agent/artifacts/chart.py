"""Turns chart_labels/chart_values into horizontal bar-fill percentages a
static document can draw.

Mirrors the chat's own bar chart (``.ai-bar-row`` in chat.html: a fixed
label column, a track, a value) rather than a vertical-column layout —
with a long or many-category label set, stacking labels under narrow
columns collides; a fixed-width label column never does.
"""

from __future__ import annotations

from dataclasses import dataclass

_MIN_PERCENT = 3.0


@dataclass(frozen=True)
class ChartBar:
    label: str
    value: float
    percent: float  # 0-100, length relative to the largest value

    @property
    def display_value(self) -> str:
        if isinstance(self.value, int) or (
            isinstance(self.value, float) and self.value.is_integer()
        ):
            return f"{int(self.value):,}"
        return f"{self.value:,.2f}"


def scale_bars(labels: list[str], values: list[float]) -> list[ChartBar]:
    if not labels or not values:
        return []
    peak = max((value or 0) for value in values) or 1
    return [
        ChartBar(
            label=str(label),
            value=value or 0,
            percent=max(_MIN_PERCENT, round((value or 0) / peak * 100, 1)),
        )
        for label, value in zip(labels, values)
    ]
