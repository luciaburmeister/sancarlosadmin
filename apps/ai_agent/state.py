"""Structured conversation memory.

Stores only dataset + ids + a short label — never a snapshot of row data.
The ERP stays the source of truth: next turn, the agent re-fetches
whatever these ids point to instead of trusting stale remembered values.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

MAX_FOCUSED_IDS = 50


@dataclass
class ActiveEntity:
    """What a pronoun like 'that client' or 'this project file' refers to."""

    dataset: str
    id: int
    label: str = ""


@dataclass
class FocusedCollection:
    """What 'those invoices' or 'the pending ones' refers to."""

    dataset: str
    ids: list[int] = field(default_factory=list)
    label: str = ""


@dataclass
class ConversationState:
    active: dict[str, ActiveEntity] = field(default_factory=dict)
    focused: FocusedCollection | None = None

    def set_active(self, dataset: str, entity_id: int, label: str = "") -> None:
        self.active[dataset] = ActiveEntity(dataset=dataset, id=entity_id, label=label)

    def set_focused(self, dataset: str, ids: list[int], label: str = "") -> None:
        self.focused = FocusedCollection(
            dataset=dataset, ids=ids[:MAX_FOCUSED_IDS], label=label
        )

    def to_dict(self) -> dict:
        return {
            "active": {key: asdict(entity) for key, entity in self.active.items()},
            "focused": asdict(self.focused) if self.focused else None,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> ConversationState:
        data = data or {}
        active = {
            key: ActiveEntity(**value)
            for key, value in (data.get("active") or {}).items()
        }
        focused_raw = data.get("focused")
        focused = FocusedCollection(**focused_raw) if focused_raw else None
        return cls(active=active, focused=focused)

    def to_prompt_summary(self) -> str:
        """Compact text injected into the system prompt every turn."""
        if not self.active and not self.focused:
            return "Nothing is active or focused yet in this conversation."

        lines: list[str] = []
        if self.active:
            lines.append(
                "Active entities (what 'that client'/'this project file' refers to):"
            )
            for entity in self.active.values():
                suffix = f" — {entity.label}" if entity.label else ""
                lines.append(f"- {entity.dataset}#{entity.id}{suffix}")
        if self.focused:
            ids_preview = ", ".join(str(i) for i in self.focused.ids[:20])
            suffix = f" — {self.focused.label}" if self.focused.label else ""
            lines.append(
                "Focused collection (what 'those'/'the pending ones' refers to): "
                f"{self.focused.dataset} ids=[{ids_preview}]{suffix}"
            )
        return "\n".join(lines)
