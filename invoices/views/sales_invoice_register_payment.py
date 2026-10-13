from invoices.models import SalesInvoice
from payments.models import PaymentDirection
from payments.views import DistributeSurplusView
from payments.views import RegisterPaymentView as BaseRegisterPaymentView


class SalesInvoicePayableConfig:
    model = SalesInvoice
    direction = PaymentDirection.INBOUND
    change_url_name = "admin:invoices_salesinvoice_change"

    def get_counterparty_filters(self, document):
        return {"client": document.client_id}

    def get_counterparty_label(self, document):
        return str(document.client)


class RegisterPaymentView(SalesInvoicePayableConfig, BaseRegisterPaymentView):
    """Sales invoice: a client is paying us, so the money comes in."""

    allow_surplus_distribution = True
    distribute_surplus_url_name = "admin:invoices_salesinvoice_distribute_surplus"
    apply_credit_url_name = "admin:invoices_salesinvoice_apply_credit"


class SalesInvoiceDistributeSurplusView(SalesInvoicePayableConfig, DistributeSurplusView):
    pass
