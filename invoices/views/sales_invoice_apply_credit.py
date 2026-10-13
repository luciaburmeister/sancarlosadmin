from invoices.views.sales_invoice_register_payment import SalesInvoicePayableConfig
from payments.views import ApplyCreditView


class SalesInvoiceApplyCreditView(SalesInvoicePayableConfig, ApplyCreditView):
    pass
