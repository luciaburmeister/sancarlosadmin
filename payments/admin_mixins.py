"""Reusable admin pieces for the counterparties behind an outstanding balance.

Client and Provider changelists answer the same questions - how much is
outstanding, how much of it is late, which invoices make it up - so both get
them from `OutstandingBalanceAdminMixin` here, configured per model.
"""
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.decorators import display
from unfold.sections import TemplateSection

from payments.mixins import (
    annotate_balance_currency,
    annotate_outstanding_balance,
    annotate_split_outstanding_balance,
    currency_symbol,
    pending_invoices_queryset,
)

# Where get_queryset() prefetches each row's pending invoices for the section.
PENDING_INVOICES_ATTR = "pending_invoices"

# Colour carries urgency, not direction: red is the overdue money and nothing
# else, so it stays the one figure that catches the eye on a row.
_OVERDUE_COLOR = "#dc2626"
_NOT_DUE_COLOR = "#6b7280"
_TOTAL_COLOR = "#111827"


class PendingInvoicesSection(TemplateSection):
    """Expandable row card breaking a counterparty's balance down invoice by invoice."""

    template_name = "payments/pending_invoices_section.html"

    def get_context_data(self, request, instance):
        invoices = getattr(instance, PENDING_INVOICES_ATTR, [])
        return {
            "invoices": [
                {
                    "invoice": invoice,
                    "url": self._change_url(invoice),
                    "is_overdue": invoice.is_overdue,
                    "remaining": invoice.db_amount_remaining,
                    "currency": currency_symbol(invoice.total_amount_currency),
                }
                for invoice in invoices
            ],
        }

    @staticmethod
    def _change_url(invoice):
        opts = invoice._meta
        return reverse(
            f"admin:{opts.app_label}_{opts.model_name}_change", args=[invoice.pk]
        )


class OutstandingBalanceAdminMixin:
    """
    Adds the overdue / not-yet-due balance columns and the pending-invoice
    dropdown to a counterparty changelist.

    Subclasses set `balance_invoice_model` (the `Payable` holding the debt),
    `balance_counterparty_field` (its FK back here) and optionally
    `balance_invoice_filters` (which invoices count at all), then add the
    balance displays to `list_display` and `PendingInvoicesSection` to
    `list_sections`.
    """

    balance_invoice_model = None
    balance_counterparty_field = None
    balance_invoice_filters: dict = {}

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = annotate_outstanding_balance(
            qs,
            self.balance_invoice_model,
            self.balance_counterparty_field,
            "balance_owed",
            invoice_filters=self.balance_invoice_filters,
        )
        qs = annotate_split_outstanding_balance(
            qs,
            self.balance_invoice_model,
            self.balance_counterparty_field,
            invoice_filters=self.balance_invoice_filters,
        )
        qs = annotate_balance_currency(
            qs,
            self.balance_invoice_model,
            self.balance_counterparty_field,
            invoice_filters=self.balance_invoice_filters,
        )
        return qs.prefetch_related(self._pending_invoices_prefetch())

    def _pending_invoices_prefetch(self):
        from django.db.models import Prefetch

        field = self.balance_invoice_model._meta.get_field(
            self.balance_counterparty_field
        )
        return Prefetch(
            field.remote_field.get_accessor_name(),
            queryset=pending_invoices_queryset(
                self.balance_invoice_model, self.balance_invoice_filters
            ),
            to_attr=PENDING_INVOICES_ATTR,
        )

    def _pending_invoices_url(self, obj, due_date_passed=None):
        """
        The invoice changelist narrowed to what this row's balance was summed
        from, so a figure links to exactly the invoices behind it.
        `due_date_passed` ("yes"/"no") picks one side of the split.
        """
        opts = self.balance_invoice_model._meta
        url = (
            reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist")
            + f"?{self.balance_counterparty_field}__id__exact={obj.pk}"
            + "&payment_status__in=unpaid,partial"
        )
        for field, value in self.balance_invoice_filters.items():
            url += f"&{field}__exact={value}"
        if due_date_passed:
            url += f"&due_date_passed={due_date_passed}"
        return url

    def _balance_link(self, obj, annotation, color, due_date_passed):
        amount = getattr(obj, annotation, None)
        if not obj or not obj.pk or not amount:
            return "—"
        symbol = currency_symbol(getattr(obj, "balance_currency", "EUR"))
        return format_html(
            # nowrap: the symbol drops to the next line once the amount gets long.
            '<a href="{}" target="_blank" rel="noopener" '
            'style="color:{};font-weight:600;white-space:nowrap;">{} {}</a>',
            self._pending_invoices_url(obj, due_date_passed), color, amount, symbol,
        )

    @display(description=_("Total balance"))
    def balance_owed_display(self, obj):
        return self._balance_link(obj, "balance_owed", _TOTAL_COLOR, None)
    balance_owed_display.admin_order_field = "balance_owed"

    @display(description=_("Overdue"))
    def balance_overdue_display(self, obj):
        return self._balance_link(obj, "balance_overdue", _OVERDUE_COLOR, "yes")
    balance_overdue_display.admin_order_field = "balance_overdue"

    @display(description=_("Not overdue"))
    def balance_not_due_display(self, obj):
        return self._balance_link(obj, "balance_not_due", _NOT_DUE_COLOR, "no")
    balance_not_due_display.admin_order_field = "balance_not_due"
