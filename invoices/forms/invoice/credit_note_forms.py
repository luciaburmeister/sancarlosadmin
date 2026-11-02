from django import forms
from django.utils.translation import gettext_lazy as _

from unfold.forms import BaseDialogForm
from unfold.widgets import UnfoldAdminSelectWidget, UnfoldAdminTextareaWidget

from invoices.models import (
    InvoicePaymentStatus,
    RectificationCode,
    RectificationReason,
    SalesInvoice,
    SalesInvoiceStatus,
)


class ReverseInvoiceForm(BaseDialogForm):
    reason = forms.ChoiceField(
        label=_("Reason"),
        choices=RectificationReason.choices,
        widget=UnfoldAdminSelectWidget,
    )
    detail = forms.CharField(
        label=_("Detail"),
        required=False,
        widget=UnfoldAdminTextareaWidget(attrs={"rows": 3}),
        help_text=_("Printed on the credit note with the reason. Required for \"Other\"."),
    )
    code = forms.ChoiceField(
        label=_("Rectification type (AEAT)"),
        choices=RectificationCode.choices,
        initial=RectificationCode.R1,
        widget=UnfoldAdminSelectWidget,
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("reason") == RectificationReason.OTHER and not (cleaned.get("detail") or "").strip():
            self.add_error("detail", _("Explain why the invoice is being rectified."))
        return cleaned


class OffsetCreditNoteForm(BaseDialogForm):
    invoice = forms.ModelChoiceField(
        label=_("Invoice"),
        queryset=SalesInvoice.objects.none(),
        widget=UnfoldAdminSelectWidget,
        help_text=_("What the client is still owed on this credit note settles this invoice, up to its balance."),
    )

    def __init__(self, request, object_id=None, *args, **kwargs):
        super().__init__(request, object_id, *args, **kwargs)
        credit_note = SalesInvoice.objects.filter(pk=object_id).first() if object_id else None
        if credit_note is not None:
            self.fields["invoice"].queryset = SalesInvoice.objects.live().filter(
                company_id=credit_note.company_id,
                client_id=credit_note.client_id,
                currency=credit_note.currency,
                status=SalesInvoiceStatus.FINAL,
                total_amount__gt=0,
                payment_status__in=[InvoicePaymentStatus.UNPAID, InvoicePaymentStatus.PARTIAL],
            ).order_by("date_issued", "pk")
