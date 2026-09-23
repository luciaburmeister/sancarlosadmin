from dataclasses import dataclass
from datetime import date

from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.translation import gettext as _

from materials.models import PaymentMethod


def _long_date(d: date) -> str:
    """'September 7, 2026' without relying on the non-portable %-d strftime flag."""
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _money(value) -> str:
    """'1234.50' -- plain, locale-independent, always two decimals."""
    return f"{value:.2f}"


@dataclass
class PaymentsForecastEmail:
    recipients: list
    subject: str
    body_html: str


class PaymentsForecastReportEmailService:
    """
    Builds the payments-forecast report email from an already-computed
    forecast (see PaymentsForecastService.build_forecast) -- this service
    only turns that data into an email, it never queries the database
    itself, so the numbers always match what the calendar shows.

    "View Invoices" links come straight from the forecast rows
    (row["view_url"], built once by Provider.invoices_admin_url) -- this
    service does not build that URL itself, so the calendar, the email,
    and their templates all share one source of truth for it.

    Unlike the Collections Forecast report, there is no reminder/claim
    action here -- these are debts to providers, not receivables from
    clients -- so each provider row only offers a "View Invoices" link
    (amount_is_link stays False in the shared template context).
    """

    TEMPLATE = "invoices/email/forecast_report.html"

    TEAL = "#0f766e"
    AMBER = "#b45309"
    TEXT = "#1f2937"

    @classmethod
    def build_email(
        cls,
        company,
        forecast: dict,
        recipients: list,
        has_logo: bool = False,
        payment_methods: list | None = None,
    ) -> PaymentsForecastEmail:
        today = forecast.get("today") or date.today()
        subject = _("Payments Forecast — %(company)s — %(date)s") % {
            "company": str(company),
            "date": today.strftime("%d/%m/%Y"),
        }

        sections = []
        if forecast["overdue"]["providers"]:
            sections.append(cls._section_context(
                title=_("Overdue"),
                total=forecast["overdue"]["total"],
                providers=forecast["overdue"]["providers"],
                title_color=cls.AMBER,
            ))

        for bucket in (b for b in forecast["buckets"] if b["count"] > 0):
            sections.append(cls._section_context(
                title=_long_date(bucket["date"]),
                total=bucket["total"],
                providers=bucket["providers"],
                title_color=cls.TEXT,
            ))

        notice_text = None
        if payment_methods:
            method_labels = dict(PaymentMethod.choices)
            labels = ", ".join(str(method_labels.get(m, m)) for m in payment_methods)
            notice_text = _("Showing only: %(methods)s") % {"methods": labels}

        company_email = getattr(company, "email", "") or ""
        company_phone = getattr(company, "phone", "") or ""
        contact_message = None
        if company_email and company_phone:
            contact_message = _("Questions? Email us at %(email)s or call %(phone)s.") % {
                "email": company_email, "phone": company_phone,
            }
        elif company_email:
            contact_message = _("Questions? Email us at %(email)s.") % {"email": company_email}
        elif company_phone:
            contact_message = _("Questions? Call us at %(phone)s.") % {"phone": company_phone}

        date_from = forecast.get("date_from") or today
        date_to = forecast.get("date_to") or today
        range_label = _("%(date_from)s – %(date_to)s") % {
            "date_from": date_from.strftime("%d/%m/%Y"),
            "date_to": date_to.strftime("%d/%m/%Y"),
        }
        base_url = settings.BACKEND_URL.rstrip("/")
        context = {
            "report_title": _("Payments Forecast"),
            "company": str(company),
            "date_label": _long_date(today),
            "intro_text": _(
                "Here is your payments forecast from %(date_from)s to %(date_to)s, "
                "plus everything already overdue:"
            ) % {"date_from": date_from.strftime("%d/%m/%Y"), "date_to": date_to.strftime("%d/%m/%Y")},
            "notice_text": notice_text,
            "kpis": [
                {"label": _("To Pay This Month"), "value": _money(forecast["month_total"]), "color": cls.TEAL},
                {"label": _("Overdue"), "value": _money(forecast["overdue"]["total"]), "color": cls.AMBER},
                {"label": range_label,
                 "value": _money(forecast.get("window_total", 0)), "color": cls.TEXT},
            ],
            "entity_label": _("Provider"),
            "sections": sections,
            "empty_text": _("No invoices are due for payment in this range."),
            "cta_url": f"{base_url}{reverse('admin:invoices_invoice_payments_forecast')}",
            "cta_label": _("View Dashboard"),
            "has_logo": has_logo,
            "contact_message": contact_message,
            "footer_text": _("This is an automated internal report generated by our payments system."),
        }

        body_html = render_to_string(cls.TEMPLATE, context)
        return PaymentsForecastEmail(recipients=recipients, subject=subject, body_html=body_html)

    @staticmethod
    def _section_context(title, total, providers, title_color) -> dict:
        return {
            "title": title,
            "title_color": title_color,
            "total": _money(total),
            "rows": [
                {
                    "name": provider["provider_name"],
                    "day_total": _money(provider["day_total"]),
                    "total_owed": _money(provider["total_owed"]),
                    "amount_is_link": False,
                    "view_url": provider["view_url"],
                }
                for provider in providers
            ],
        }
