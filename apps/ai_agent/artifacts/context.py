"""Shared, typed payload the agent's artifact builders render from.

Built once in ``GenerateArtifact.execute()``, consumed identically by the
PDF/Excel/PPTX builders — keeps them rendering off one dataclass instead
of a growing list of positional args. A document holds one or more
``ArtifactSection``s, each scoped to a single dataset query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class KpiTile:
    label: str
    value: float | int

    @property
    def display_value(self) -> str:
        if isinstance(self.value, int) or (
            isinstance(self.value, float) and self.value.is_integer()
        ):
            return f"{int(self.value):,}"
        return f"{self.value:,.2f}"


@dataclass(frozen=True)
class ArtifactSection:
    title: str
    columns: list[str]
    rows: list[dict]
    highlights: list[str] = field(default_factory=list)
    filters_summary: list[str] = field(default_factory=list)
    kpis: list[KpiTile] = field(default_factory=list)
    chart_labels: list[str] = field(default_factory=list)
    chart_values: list[float] = field(default_factory=list)
    chart_type: str = "none"

    @property
    def has_chart(self) -> bool:
        return self.chart_type != "none" and bool(self.chart_labels)


@dataclass(frozen=True)
class ArtifactContext:
    title: str
    company_label: str
    generated_at: datetime
    summary: str = ""
    sections: list[ArtifactSection] = field(default_factory=list)
