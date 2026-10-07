from datetime import timedelta

from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from core.admin.company_access import get_user_companies
from invoices.models import Invoice
from invoices.services.payments_forecast_service import PaymentsForecastService
from invoices.tasks import send_payments_forecast_report_email
from materials.models import PaymentMethod


class PaymentsForecastView(UnfoldModelAdminViewMixin, TemplateView):
    """
    Payments forecast calendar ("previsión de pagos a proveedores"): an
    overdue bucket followed by one row per upcoming due date, each broken
    down by provider with the amount due, their total outstanding balance,
    and a link to view their invoices.

    Unlike Collections Forecast, there is no reminder/claim action here --
    these are debts to providers, not receivables from clients -- so each
    provider row only offers "View Invoices".

    Read-only over Invoice -- all query logic lives in
    PaymentsForecastService.build_forecast(), shared with the
    scheduled/manual email report so both always show the same numbers.

    Also handles the "Send report now" button (POST): queues the same
    email task used by the scheduled Celery beat job, for whichever
    company/window/payment-method filter is currently on screen -- it
    sends exactly what the user is looking at, not the company's saved
    default settings.

    """

    title = _("Payments Forecast")
    permission_required = ("invoices.payments_forecast",)
    template_name = "admin/invoices/invoice/payments_forecast.html"
    model_admin = None

    # Default range shown when the user hasn't picked dates yet, and the
    # largest span we'll ever compute -- same cap the old "days ahead"
    # field used, just expressed as a date range now.
    DEFAULT_SPAN_DAYS = 14
    MAX_SPAN_DAYS = 180

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request

        selected_company = self._resolve_company(request)
        date_from, date_to = self._resolve_date_range(request)
        selected_methods = request.GET.getlist("payment_method")
        forecast = PaymentsForecastService.build_forecast(
            selected_company, date_from, date_to, selected_methods or None
        )

        ctx.update({
            "title": _("Payments Forecast"),
            "opts": Invoice._meta,
            "companies": get_user_companies(request.user),
            "selected_company": selected_company,
            "date_from": date_from,
            "date_to": date_to,
            "payment_method_choices": PaymentMethod.choices,
            "selected_methods": selected_methods,
            "forecast": forecast,
            "today": timezone.localdate(),
        })
        return ctx

    def post(self, request, *args, **kwargs):
        """Manual "Send report now" -- queues the exact same task the
        scheduled beat job uses, for the company/date range/payment-method
        filter currently on screen."""
        selected_company = self._resolve_company(request, source=request.POST)
        date_from, date_to = self._resolve_date_range(request, source=request.POST)
        selected_methods = request.POST.getlist("payment_method")

        if not selected_company:
            self.model_admin.message_user(request, _("Select a company first."), level="error")
        else:
            send_payments_forecast_report_email.delay(
                selected_company.pk, date_from.isoformat(), date_to.isoformat(), selected_methods or None
            )
            self.model_admin.message_user(
                request,
                _("Report queued for %(company)s -- it will arrive by email shortly.")
                % {"company": selected_company},
            )

        redirect_url = reverse("admin:invoices_invoice_payments_forecast")
        params = []
        if selected_company:
            params.append(f"company={selected_company.pk}")
        params.append(f"date_from={date_from.isoformat()}")
        params.append(f"date_to={date_to.isoformat()}")
        for method in selected_methods:
            params.append(f"payment_method={method}")
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

    def _resolve_date_range(self, request, source=None):
        source = source or request.GET
        today = timezone.localdate()

        date_from = parse_date(source.get("date_from", "")) or today
        date_to = parse_date(source.get("date_to", "")) or today + timedelta(days=self.DEFAULT_SPAN_DAYS)

        if date_to < date_from:
            date_from, date_to = date_to, date_from

        if (date_to - date_from).days > self.MAX_SPAN_DAYS:
            date_to = date_from + timedelta(days=self.MAX_SPAN_DAYS)

        return date_from, date_to
