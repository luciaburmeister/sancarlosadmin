from invoices.views.invoice_register_payment import InvoicePayableConfig
from payments.views import ApplyCreditView


class InvoiceApplyCreditView(InvoicePayableConfig, ApplyCreditView):
    pass
