from datetime import timedelta

from django.urls import reverse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.translation import gettext_lazy as _
from django.views import View
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
        forecast = CollectionsForecastService.build_forecast(selected_company, date_from, date_to)

        ctx.update({
            "title": _("Collections Forecast"),
            "opts": SalesInvoice._meta,
            "companies": get_user_companies(request.user),
            "selected_company": selected_company,
            "date_from": date_from,
            "date_to": date_to,
            "forecast": forecast,
            "today": timezone.localdate(),
        })
        return ctx

    def post(self, request, *args, **kwargs):
        """Manual "Send report now" -- queues the exact same task the
        scheduled beat job uses, for the company/date range on screen."""
        selected_company = self._resolve_company(request, source=request.POST)
        date_from, date_to = self._resolve_date_range(request, source=request.POST)

        if not selected_company:
            self.model_admin.message_user(request, _("Select a company first."), level="error")
        else:
            send_collections_forecast_report_email.delay(
                selected_company.pk, date_from.isoformat(), date_to.isoformat()
            )
            self.model_admin.message_user(
                request,
                _("Report queued for %(company)s -- it will arrive by email shortly.")
                % {"company": selected_company},
            )

        redirect_url = reverse("admin:invoices_salesinvoice_collections_forecast")
        params = []
        if selected_company:
            params.append(f"company={selected_company.pk}")
        params.append(f"date_from={date_from.isoformat()}")
        params.append(f"date_to={date_to.isoformat()}")
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


class CollectionsForecastSetCommitmentDateView(UnfoldModelAdminViewMixin, View):
    """
    "Edit Commitment Date" action from the Collections Forecast calendar:
    bulk-sets payment_commitment_date on every invoice behind one client's
    row in one bucket, so the whole row moves together instead of leaving
    some of its invoices on the old date.

    Which invoices that is comes straight from
    CollectionsForecastService.row_invoices() -- the same condition the
    row's total and its "View Invoices" link were built from -- so this
    action can never touch a different set of invoices than what the row
    on screen represents.
    """

    permission_required = ("invoices.collections_forecast",)

    def post(self, request, *args, **kwargs):
        user_companies = get_user_companies(request.user)
        try:
            company = user_companies.get(pk=request.POST.get("company"))
        except (ValueError, TypeError, user_companies.model.DoesNotExist):
            company = None

        client_id = request.POST.get("client_id")
        commitment_date = parse_date(request.POST.get("commitment_date", ""))
        forecast_date = parse_date(request.POST.get("forecast_date", ""))
        overdue_before = parse_date(request.POST.get("overdue_before", ""))
        date_from = parse_date(request.POST.get("date_from", ""))
        date_to = parse_date(request.POST.get("date_to", ""))

        if not (company and client_id and commitment_date and (forecast_date or overdue_before)):
            self.model_admin.message_user(
                request, _("Missing or invalid data -- commitment date not updated."), level="error"
            )
        else:
            invoices = CollectionsForecastService.row_invoices(
                company, client_id, forecast_date=forecast_date, overdue_before=overdue_before
            )
            updated = invoices.update(payment_commitment_date=commitment_date)
            if date_from and date_to:
                CollectionsForecastService.invalidate_cache(company, date_from, date_to)
            self.model_admin.message_user(
                request,
                _("Updated the commitment date on %(count)s invoice(s) to %(date)s.")
                % {"count": updated, "date": commitment_date.strftime("%d/%m/%Y")},
            )

        redirect_url = reverse("admin:invoices_salesinvoice_collections_forecast")
        params = []
        if company:
            params.append(f"company={company.pk}")
        if date_from:
            params.append(f"date_from={date_from.isoformat()}")
        if date_to:
            params.append(f"date_to={date_to.isoformat()}")
        return redirect(f"{redirect_url}?{'&'.join(params)}")
