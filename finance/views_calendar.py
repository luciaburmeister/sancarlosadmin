# finance/views_calendar.py
from __future__ import annotations

import datetime
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView
from unfold.views import UnfoldModelAdminViewMixin

from company.models import BankAccount
from core.admin.company_access import get_user_companies
from core.dates import week_bounds

from .forms_calendar import PaymentCalendarFilterForm
from .services.payment_calendar import build_calendar_matrix

MAX_WINDOW_DAYS = 14


class PaymentCalendarView(UnfoldModelAdminViewMixin, TemplateView):
    title = "Calendario de Pagos"
    permission_required = ()
    template_name = "admin/finance/payment_calendar.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        companies = get_user_companies(request.user)

        raw_company_id = request.GET.get("company") or None
        company = companies.filter(pk=raw_company_id).first() if raw_company_id else None
        company = company or companies.first()

        company_bank_accounts = BankAccount.objects.filter(company=company) if company else BankAccount.objects.none()

        # An empty selection means "all accounts" — this must stay empty
        # when building URLs/form initial, and only get expanded to the
        # full set at query time, so navigation never silently narrows
        # the filter to specific accounts the user never chose.
        selected_account_ids = [account_id for account_id in request.GET.getlist("bank_accounts") if account_id]
        selected_bank_accounts = (
            company_bank_accounts.filter(pk__in=selected_account_ids)
            if selected_account_ids
            else company_bank_accounts
        )

        today = timezone.localdate()

        date_from_str = request.GET.get("date_from")
        date_to_str = request.GET.get("date_to")

        try:
            date_from = datetime.date.fromisoformat(date_from_str) if date_from_str else None
        except ValueError:
            date_from = None

        try:
            date_to = datetime.date.fromisoformat(date_to_str) if date_to_str else None
        except ValueError:
            date_to = None

        if not date_from or not date_to:
            date_from = today - datetime.timedelta(days=today.weekday())
            date_to = date_from + datetime.timedelta(days=6)

        if date_from > date_to:
            date_from, date_to = date_to, date_from

        range_was_truncated = False
        max_date_to = date_from + datetime.timedelta(days=MAX_WINDOW_DAYS - 1)
        if date_to > max_date_to:
            date_to = max_date_to
            range_was_truncated = True

        form = PaymentCalendarFilterForm(
            data=request.GET if request.GET else None,
            companies=companies,
            bank_accounts=company_bank_accounts,
            initial={
                "company": company.pk if company else None,
                "bank_accounts": selected_account_ids,
                "date_from": date_from,
                "date_to": date_to,
            },
        )
        if request.GET:
            form.data = form.data.copy()
            form.data["date_from"] = date_from.isoformat()
            form.data["date_to"] = date_to.isoformat()

        matrix = build_calendar_matrix(company, selected_bank_accounts, date_from, date_to) if company else None

        base_url = reverse("admin:finance_paymentcalendar_changelist")

        def _build_url(window_date_from: datetime.date, window_date_to: datetime.date) -> str:
            params = [("company", company.pk if company else "")]
            params += [("bank_accounts", account_id) for account_id in selected_account_ids]
            params += [
                ("date_from", window_date_from.isoformat()),
                ("date_to", window_date_to.isoformat()),
            ]
            return f"{base_url}?{urlencode(params)}"

        window_length = (date_to - date_from).days + 1
        prev_date_from = date_from - datetime.timedelta(days=window_length)
        prev_date_to = date_to - datetime.timedelta(days=window_length)
        next_date_from = date_from + datetime.timedelta(days=window_length)
        next_date_to = date_to + datetime.timedelta(days=window_length)

        today_week_start, today_week_end = week_bounds(today)

        context.update({
            "form": form,
            "selected_company": company,
            "date_from": date_from,
            "date_to": date_to,
            "today": today,
            "matrix": matrix,
            "range_was_truncated": range_was_truncated,
            "max_window_days": MAX_WINDOW_DAYS,
            "prev_week_url": _build_url(prev_date_from, prev_date_to),
            "next_week_url": _build_url(next_date_from, next_date_to),
            "today_url": _build_url(today_week_start, today_week_end),
        })
        return context