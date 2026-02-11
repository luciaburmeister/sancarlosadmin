from django import forms

from invoices.models import Invoice
from unfold.widgets import UnfoldAdminFileFieldWidget


class InvoiceUploadForm(forms.ModelForm):
    class Meta:
        model = Invoice
        fields = ("file",)
        widgets = {
            "file": UnfoldAdminFileFieldWidget(attrs={"accept": ".pdf,.jpeg,.jpg"}),
        }
        class Media:
            js = [
            f"admin/js/vendor/jquery/jquery.js",
            "admin/js/jquery.init.js",
            "admin/js/calendar.js",
            "admin/js/admin/DateTimeShortcuts.js",
            "admin/js/core.js",
        ]