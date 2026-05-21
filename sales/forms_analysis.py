from django import forms
from django.utils.translation import gettext_lazy as _

from unfold.widgets import (
    INPUT_CLASSES,
    UnfoldAdminDateWidget,
    UnfoldAdminSelect2Widget,
    UnfoldAdminSelect2MultipleWidget,
)

_INPUT_CLS = " ".join(INPUT_CLASSES)

from .analysis import ALL_DIAGNOSES


class ConversionAnalysisFilterForm(forms.Form):

    class Media:
        js = [
            "admin/js/vendor/jquery/jquery.js",
            "admin/js/jquery.init.js",
            "admin/js/calendar.js",
            "admin/js/admin/DateTimeShortcuts.js",
            "admin/js/core.js",
        ]

    # ── 1. Empresa ────────────────────────────────────────────────────────────
    company = forms.ChoiceField(
        required=False,
        label=_("Empresa"),
        choices=[],
        widget=UnfoldAdminSelect2Widget(),
    )

    # ── 2. Artículo ───────────────────────────────────────────────────────────
    q = forms.ChoiceField(
        required=False,
        label=_("Artículo"),
        choices=[],
        widget=UnfoldAdminSelect2Widget(
            attrs={"data-allow-clear": "true"}
        ),
    )

    # ── 3. Mínimo Presupuestos ────────────────────────────────────────────────
    min_quoted = forms.FloatField(
        required=False,
        label=_("Mínimo Presupuestos"),
        min_value=0,
        widget=forms.NumberInput(
            attrs={
                "class": _INPUT_CLS,
                "step": "1",
            }
        ),
    )

    # ── 4. Estado ─────────────────────────────────────────────────────────────
    diagnosis = forms.MultipleChoiceField(
        required=False,
        label=_("Estado"),
        choices=[(d, d) for d in ALL_DIAGNOSES],
        widget=UnfoldAdminSelect2MultipleWidget(),
    )

    # ── 5. Desde ──────────────────────────────────────────────────────────────
    date_from = forms.DateField(
        required=False,
        label=_("Desde"),
        widget=UnfoldAdminDateWidget,
    )

    # ── 6. Hasta ──────────────────────────────────────────────────────────────
    date_to = forms.DateField(
        required=False,
        label=_("Hasta"),
        widget=UnfoldAdminDateWidget,
    )

    def __init__(self, *args, companies=None, company_id=None, **kwargs):
        super().__init__(*args, **kwargs)

        # ── Company choices ───────────────────────────────────────────────────
        company_choices = [("", _("Todas las empresas"))]
        if companies is not None:
            company_choices += [(str(c.pk), str(c)) for c in companies]
        self.fields["company"].choices = company_choices

        # ── Article choices — only items that appear in quote items ───────────
        from .models import QuoteItem

        company_filter = {"quote__company_id": company_id} if company_id else (
            {"quote__company__in": companies} if companies is not None else {}
        )
        qi_rows = (
            QuoteItem.objects
            .filter(**company_filter)
            .exclude(material__isnull=True)
            .values_list("material__reference_code", "material__name")
            .distinct()
            .order_by("material__name")
        )

        from configurator.models import ConfigurationItem
        config_filter = {"configuration__company_id": company_id} if company_id else (
            {"configuration__company__in": companies} if companies is not None else {}
        )
        ci_rows = (
            ConfigurationItem.objects
            .filter(**config_filter, configuration__quote__isnull=False)
            .exclude(component__isnull=True)
            .values_list("component__code", "component__name")
            .distinct()
            .order_by("component__name")
        )

        seen = set()
        article_choices = [("", "")]
        for ref, name in list(qi_rows) + list(ci_rows):
            if ref and ref not in seen:
                seen.add(ref)
                article_choices.append((ref, f"{ref} — {name}"))

        self.fields["q"].choices = article_choices
