"""
A readiness check: one thing a company has or still lacks, and where to fix it.

Checks are grouped in sections. A section, and anything built from sections,
is as bad as its worst check: missing beats warning beats ok.
"""
from dataclasses import dataclass, field
from typing import List, Optional

OK, WARNING, MISSING = "ok", "warning", "missing"


@dataclass
class Check:
    label: str
    status: str
    detail: str = ""
    url: Optional[str] = None


@dataclass
class Section:
    title: str
    checks: List[Check] = field(default_factory=list)

    @property
    def status(self):
        return worst(check.status for check in self.checks)


def worst(statuses):
    statuses = set(statuses)
    return MISSING if MISSING in statuses else WARNING if WARNING in statuses else OK
