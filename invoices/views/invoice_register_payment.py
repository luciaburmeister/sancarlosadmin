from invoices.models import Invoice
from payments.models import PaymentDirection
from payments.views import RegisterPaymentView


class InvoiceRegisterPaymentView(RegisterPaymentView):
    """Purchase invoice: paying a provider, so the money goes out."""

    model = Invoice
    direction = PaymentDirection.OUTBOUND
    change_url_name = "admin:invoices_invoice_change"
