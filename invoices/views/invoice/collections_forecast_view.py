from django.urls import reverse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from core.admin.company_access import get_user_companies
from invoices.models import SalesInvoice
from invoices.services.collections_forecast_service import CollectionsForecastService
from invoices.tasks import send_collections_forecast_report_email


class CollectionsForecastView(UnfoldModelAdminViewMixin, TemplateView):
    """
    Collections forecast calendar ("previsión de cobros"): an overdue
    bucket followed by one row per upcoming due date, each broken down
    by client with the amount due, their total outstanding balance, and
    links to view their invoices or send a payment reminder.

    Read-only over SalesInvoice -- all query logic lives in
    CollectionsForecastService.build_forecast(), shared with the
    scheduled/manual email report so both always show the same numbers.

    Also handles the "Send report now" button (POST): queues the same
    email task used by the scheduled Celery beat job, for whichever
    company/window is currently on screen.
    """

    title = _("Collections Forecast")
    permission_required = ("invoices.collections_forecast",)
    template_name = "admin/invoices/invoice/collections_forecast.html"
    model_admin = None

    MIN_WINDOW = 1
    MAX_WINDOW = 180
    DEFAULT_WINDOW = 14

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request

        selected_company = self._resolve_company(request)
        days_ahead = self._resolve_days(request)
        forecast = CollectionsForecastService.build_forecast(selected_company, days_ahead)

        ctx.update({
            "title": _("Collections Forecast"),
            "opts": SalesInvoice._meta,
            "companies": get_user_companies(request.user),
            "selected_company": selected_company,
            "days_ahead": days_ahead,
            "days_min": self.MIN_WINDOW,
            "days_max": self.MAX_WINDOW,
            "forecast": forecast,
            "today": timezone.now().date(),
        })
        return ctx

    def post(self, request, *args, **kwargs):
        """Manual "Send report now" -- queues the exact same task the
        scheduled beat job uses, for the company/window on screen."""
        selected_company = self._resolve_company(request, source=request.POST)
        days_ahead = self._resolve_days(request, source=request.POST)

        if not selected_company:
            self.model_admin.message_user(request, _("Select a company first."), level="error")
        else:
            send_collections_forecast_report_email.delay(selected_company.pk, days_ahead)
            self.model_admin.message_user(
                request,
                _("Report queued for %(company)s -- it will arrive by email shortly.")
                % {"company": selected_company},
            )

        redirect_url = reverse("admin:invoices_salesinvoice_collections_forecast")
        params = []
        if selected_company:
            params.append(f"company={selected_company.pk}")
        params.append(f"days={days_ahead}")
        return redirect(f"{redirect_url}?{'&'.join(params)}")


    def _resolve_company(self, request, source=None):
        source = source or request.GET
        user_companies = get_user_companies(request.user)
        try:
            company_id = int(source.get("company")) if source.get("company") else None
        except (ValueError, TypeError):
            company_id = None

        if company_id and user_companies.filter(id=company_id).exists():
            return user_companies.get(id=company_id)
        return user_companies.first()

    def _resolve_days(self, request, source=None):
        source = source or request.GET
        try:
            days_ahead = int(source.get("days", self.DEFAULT_WINDOW))
        except (ValueError, TypeError):
            return self.DEFAULT_WINDOW
        return max(self.MIN_WINDOW, min(days_ahead, self.MAX_WINDOW))
