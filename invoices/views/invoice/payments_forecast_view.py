from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
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

    Shares the "invoices.accounts_payable" permission with the Accounts
    Payable dashboard: both are entry points into the same accounts-payable
    workflow, just at different levels of detail.
    """

    title = _("Payments Forecast")
    permission_required = ("invoices.accounts_payable",)
    template_name = "admin/invoices/invoice/payments_forecast.html"
    model_admin = None

    MIN_WINDOW = 1
    MAX_WINDOW = 180
    DEFAULT_WINDOW = 14

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        request = self.request

        selected_company = self._resolve_company(request)
        days_ahead = self._resolve_days(request)
        selected_methods = request.GET.getlist("payment_method")
        forecast = PaymentsForecastService.build_forecast(
            selected_company, days_ahead, selected_methods or None
        )

        ctx.update({
            "title": _("Payments Forecast"),
            "opts": Invoice._meta,
            "companies": get_user_companies(request.user),
            "selected_company": selected_company,
            "days_ahead": days_ahead,
            "days_min": self.MIN_WINDOW,
            "days_max": self.MAX_WINDOW,
            "payment_method_choices": PaymentMethod.choices,
            "selected_methods": selected_methods,
            "forecast": forecast,
            "today": timezone.now().date(),
        })
        return ctx

    def post(self, request, *args, **kwargs):
        """Manual "Send report now" -- queues the exact same task the
        scheduled beat job uses, for the company/window/payment-method
        filter currently on screen."""
        selected_company = self._resolve_company(request, source=request.POST)
        days_ahead = self._resolve_days(request, source=request.POST)
        selected_methods = request.POST.getlist("payment_method")

        if not selected_company:
            self.model_admin.message_user(request, _("Select a company first."), level="error")
        else:
            send_payments_forecast_report_email.delay(
                selected_company.pk, days_ahead, selected_methods or None
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
        params.append(f"days={days_ahead}")
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

    def _resolve_days(self, request, source=None):
        source = source or request.GET
        try:
            days_ahead = int(source.get("days", self.DEFAULT_WINDOW))
        except (ValueError, TypeError):
            return self.DEFAULT_WINDOW
        return max(self.MIN_WINDOW, min(days_ahead, self.MAX_WINDOW))
