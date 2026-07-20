from django import forms
from django.utils.translation import gettext_lazy as _

from unfold.forms import BaseDialogForm
from unfold.widgets import UnfoldAdminTextareaWidget


class ExpenseClaimRejectForm(BaseDialogForm):
    rejection_reason = forms.CharField(
        label=_("Rejection Reason"),
        widget=UnfoldAdminTextareaWidget(attrs={"rows": 4}),
    )
