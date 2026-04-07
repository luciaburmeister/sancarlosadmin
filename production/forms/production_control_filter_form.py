from django import forms
from django.utils.translation import gettext_lazy as _

from production.models import ProductionItemSession
from unfold.widgets import UnfoldAdminSelectWidget, UnfoldAdminTextInputWidget


class ProductionControlFilterForm(forms.Form):
    q = forms.CharField(
        required=False,
        label="",
        widget=UnfoldAdminTextInputWidget(
            attrs={
                "placeholder": _("Search employee, stage, ticket or component"),
            }
        ),
    )

    stage = forms.ChoiceField(
        required=False,
        label="Stage",
        choices=[],
        widget=UnfoldAdminSelectWidget(),
    )

    def __init__(self, *args, **kwargs):
        kwargs.pop("admin_site", None)
        super().__init__(*args, **kwargs)

        stage_choices = list(
            ProductionItemSession.objects.exclude(production_stage__isnull=True)
            .exclude(production_stage="")
            .values_list("production_stage", flat=True)
            .distinct()
            .order_by("production_stage")
        )

        self.fields["stage"].choices = [("", _("All stages"))] + [(s, s) for s in stage_choices]