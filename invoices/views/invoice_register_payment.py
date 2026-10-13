from invoices.models import Invoice
from payments.models import PaymentDirection
from payments.views import DistributeSurplusView, RegisterPaymentView


class InvoicePayableConfig:
    model = Invoice
    direction = PaymentDirection.OUTBOUND
    change_url_name = "admin:invoices_invoice_change"

    def get_counterparty_filters(self, document):
        return {"provider": document.provider_id}

    def get_counterparty_label(self, document):
        return str(document.provider)


class InvoiceRegisterPaymentView(InvoicePayableConfig, RegisterPaymentView):
    """Purchase invoice: paying a provider, so the money goes out."""

    allow_surplus_distribution = True
    distribute_surplus_url_name = "admin:invoices_invoice_distribute_surplus"
    apply_credit_url_name = "admin:invoices_invoice_apply_credit"


class InvoiceDistributeSurplusView(InvoicePayableConfig, DistributeSurplusView):
    pass
