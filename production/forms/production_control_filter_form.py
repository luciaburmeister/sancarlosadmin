from django import forms
from django.utils.translation import gettext_lazy as _

from company.models import Company
from unfold.widgets import UnfoldAdminSelect2Widget, UnfoldAdminTextInputWidget
from production.models import ProductionItemSession


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

    company = forms.ModelChoiceField(
        required=False,
        label=_("Company"),
        queryset=Company.objects.filter(is_active=True).order_by("name"),
        empty_label=_("All companies"),
        widget=UnfoldAdminSelect2Widget(),
    )

    stage = forms.ChoiceField(
        required=False,
        label=_("Stage"),
        choices=[],
        widget=UnfoldAdminSelect2Widget(),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        stages = (
            ProductionItemSession.objects.order_by()
            .values_list("production_stage", flat=True)
            .distinct()
        )

        self.fields["stage"].choices = [("", _("All stages"))] + [
            (stage, stage) for stage in stages if stage
        ]