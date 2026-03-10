from django.utils.translation import gettext as _
from unfold.components import BaseComponent, register_component


@register_component
class StatusBadge(BaseComponent):

    STATUS_COLORS = {
        "pending": "#9ca3af",       # gray
        "in_progress": "#f59e0b",   # amber
        "done": "#10b981",          # green
        "cancelled": "#ef4444",     # red
    }

    def get_context_data(self, status=None, **kwargs):
        context = super().get_context_data(**kwargs)

        status = (status or "").strip()
        color = self.STATUS_COLORS.get(status, "#6b7280")

        label = _(status.replace("_", " ")) if status else "—"

        context.update({
            "color": color,
            "label": label,
        })
        return context