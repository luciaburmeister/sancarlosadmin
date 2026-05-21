"""
sales/views_analysis.py
─────────────────────────────────────────────────────────────────────────────
Quote-to-sale conversion analysis page, wired into Unfold via
UnfoldModelAdminViewMixin so it renders with the full sidebar, header and
theme automatically.
"""

from __future__ import annotations

from django.core.cache import cache
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from core.admin.company_access import get_user_companies
from .analysis import run_conversion_analysis, get_company_wide_avg, DIAGNOSIS_CSS
from .forms_analysis import ConversionAnalysisFilterForm

CACHE_TTL = 60 * 60  # 1 hour


class ConversionAnalysisView(UnfoldModelAdminViewMixin, TemplateView):
    title = "Análisis de Conversión de Ventas"
    permission_required = ()          # any logged-in staff member may view
    template_name = "admin/sales/conversion_analysis.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        companies = get_user_companies(request.user)

        # ── Company selection (raw GET first so form choices load correctly) ──
        raw_company_id = request.GET.get("company") or None
        if raw_company_id:
            try:
                company = companies.get(pk=raw_company_id)
            except Exception:
                company = companies.first()
        else:
            company = companies.first()

        # ── Build form with company-scoped material choices ───────────────────
        form = ConversionAnalysisFilterForm(
            data=request.GET if request.GET else None,
            companies=companies,
            company_id=company.pk if company else None,
        )
        form.is_valid()  # always call so cleaned_data is populated
        cd = getattr(form, "cleaned_data", {})

        # ── Filter params ─────────────────────────────────────────────────────
        date_from        = cd.get("date_from")
        date_to          = cd.get("date_to")
        search_query     = cd.get("q") or ""
        diagnosis_filter = cd.get("diagnosis") or []
        min_quoted       = cd.get("min_quoted")

        # ── Sort param ────────────────────────────────────────────────────────
        SORT_OPTIONS = {
            "conv_asc":    (lambda r: r["conversion_pct"],          False),
            "conv_desc":   (lambda r: r["conversion_pct"],          True),
            "quoted_desc": (lambda r: r["quoted_qty"],              True),
            "sold_desc":   (lambda r: r["sold_qty"],                True),
            "margin_desc": (lambda r: r["avg_margin_pct"] or -9999, True),
            "margin_asc":  (lambda r: r["avg_margin_pct"] or -9999, False),
        }
        sort_key = request.GET.get("sort", "conv_asc")
        if sort_key not in SORT_OPTIONS:
            sort_key = "conv_asc"
        sort_fn, sort_reverse = SORT_OPTIONS[sort_key]

        cache_key = (
            f"conversion_analysis"
            f":company={company.pk if company else 'none'}"
            f":from={date_from}"
            f":to={date_to}"
            f":q={search_query}"
            f":diag={'_'.join(sorted(diagnosis_filter))}"
            f":min={min_quoted}"
        )
        company_wide_cache_key = f"conversion_analysis:company_wide:{company.pk if company else 'none'}"

        result         = cache.get(cache_key)
        company_wide_avg = cache.get(company_wide_cache_key)

        if result is None:
            result = {"summary": None, "rows": []}
            if company:
                result = run_conversion_analysis(
                    company=company,
                    date_from=date_from,
                    date_to=date_to,
                    diagnosis_filter=diagnosis_filter if diagnosis_filter else None,
                    search_query=search_query or None,
                    min_quoted=min_quoted,
                )
                cache.set(cache_key, result, CACHE_TTL)

        if company_wide_avg is None and company:
            company_wide_avg = get_company_wide_avg(company)
            cache.set(company_wide_cache_key, company_wide_avg, CACHE_TTL)

        rows = list(result.get("rows", []))
        rows.sort(key=sort_fn, reverse=sort_reverse)

        context.update({
            "form":             form,
            "summary":          result["summary"],
            "rows":             rows,
            "companies":        companies,
            "selected_company": company,
            "diagnosis_css":    DIAGNOSIS_CSS,
            "company_wide_avg": company_wide_avg,
            "current_sort":     sort_key,
            "sort_options": [
                ("conv_asc",    "Peor conversión primero"),
                ("conv_desc",   "Mejor conversión primero"),
                ("quoted_desc", "Más cotizados"),
                ("sold_desc",   "Más vendidos"),
                ("margin_desc", "Mayor margen"),
                ("margin_asc",  "Menor margen"),
            ],
        })
        return context