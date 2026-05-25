from django import forms
from django.core.validators import FileExtensionValidator
from django.utils.translation import gettext_lazy as _
from unfold.widgets import (
    UnfoldAdminFileFieldWidget,
    UnfoldAdminIntegerFieldWidget,
    UnfoldAdminTextInputWidget,
)


class SalesInvoiceImportForm(forms.Form):
    file = forms.FileField(
        label=_("Excel File"),
        validators=[FileExtensionValidator(allowed_extensions=["xlsx", "xls"])],
        widget=UnfoldAdminFileFieldWidget(attrs={"accept": ".xlsx,.xls"}),
        help_text=_(
            "Upload an Excel file with sales invoice data. "
            "Required columns: invoice_number, date_issued, client_cif, base_amount, company."
        ),
    )
    default_year = forms.IntegerField(
        label=_("Default Year"),
        required=False,
        widget=UnfoldAdminIntegerFieldWidget(),
        help_text=_(
            "Year to use for dates without a year (e.g., DD/MM format). "
            "Leave blank to use the current year."
        ),
    )
    sheet_name = forms.CharField(
        label=_("Sheet Name"),
        required=False,
        widget=UnfoldAdminTextInputWidget(),
        help_text=_(
            "Name of the Excel sheet to import. "
            "Leave blank to use the active/first sheet."
        ),
    )

    class Media:
        js = [
            "admin/js/vendor/jquery/jquery.js",
            "admin/js/jquery.init.js",
            "admin/js/core.js",
        ]
