from invoices.models import SalesInvoice
from payments.models import PaymentDirection
from payments.views import RegisterPaymentView as BaseRegisterPaymentView


class RegisterPaymentView(BaseRegisterPaymentView):
    """Sales invoice: a client is paying us, so the money comes in."""

    model = SalesInvoice
    direction = PaymentDirection.INBOUND
    change_url_name = "admin:invoices_salesinvoice_change"
