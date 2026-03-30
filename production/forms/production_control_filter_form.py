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

    company = forms.ChoiceField(
        required=False,
        label="Company",
        choices=[],
        widget=UnfoldAdminSelectWidget(),
    )

    stage = forms.ChoiceField(
        required=False,
        label="Stage",
        choices=[],
        widget=UnfoldAdminSelectWidget(),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        company_choices = list(
            ProductionItemSession.objects.exclude(company__isnull=True)
            .select_related("company")
            .values_list("company_id", "company__name")
            .distinct()
            .order_by("company__name")
        )

        stage_choices = list(
            ProductionItemSession.objects.exclude(production_stage__isnull=True)
            .exclude(production_stage="")
            .values_list("production_stage", flat=True)
            .distinct()
            .order_by("production_stage")
        )

        self.fields["company"].choices = [("", _("All companies"))] + company_choices
        self.fields["stage"].choices = [("", _("All stages"))] + [(s, s) for s in stage_choices]