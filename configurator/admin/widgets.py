import json

from django.core.validators import EMPTY_VALUES
from django.forms import Widget


class SortableArrayWidget(Widget):
    """
    Edits a Postgres ArrayField of strings (optionally choice-restricted) as
    a list of rows that can be reordered by dragging, added to, and removed
    from with the mouse - instead of deleting entries and re-adding them to
    change their order.

    Submits the same wire format as Unfold's ArrayWidget it replaces
    (repeated inputs sharing `name`, read back in DOM order), so no model or
    migration changes are involved - only the admin UI changes.
    """

    template_name = "configurator/admin/widgets/sortable_array.html"

    class Media:
        js = ("js/admin/sortable_array_widget.js",)

    def __init__(self, choices=None, attrs=None):
        self.choices = list(choices) if choices is not None else None
        super().__init__(attrs)

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context["widget"]["items_json"] = json.dumps(self._as_list(value))
        context["widget"]["choices"] = self.choices
        return context

    @staticmethod
    def _as_list(value):
        if not value:
            return []
        if isinstance(value, list):
            return value
        return [v for v in str(value).split(",") if v]

    def value_from_datadict(self, data, files, name):
        return [v for v in data.getlist(name) if v not in EMPTY_VALUES]

    def value_omitted_from_data(self, data, files, name):
        return data.getlist(name) not in [[""], *EMPTY_VALUES]
