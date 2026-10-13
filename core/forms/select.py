from django import forms
from unfold.widgets import UnfoldAdminSelect2MultipleWidget


class StaticSelect2MultipleWidget(UnfoldAdminSelect2MultipleWidget):
    """Select2 multi-select with local options (search field and chips).

    UnfoldAdminSelect2MultipleWidget adds ``admin-autocomplete``, which
    expects an AJAX endpoint and hides these choices.
    """

    def __init__(self, attrs=None, choices=()):
        attrs = attrs or {}
        attrs["data-theme"] = "admin-autocomplete"
        attrs["class"] = "unfold-admin-autocomplete"
        forms.SelectMultiple.__init__(self, attrs, choices)

    class Media(UnfoldAdminSelect2MultipleWidget.Media):
        pass
