"""
Shared registry mapping model class → export strategy class.

Mirrors kind_registry.py: lazy imports avoid circular imports at module
level. New exportable documents only need one entry here.
"""

from .configuration import ConfigurationExportStrategy
from .production_item import ProductionItemExportStrategy
from .sales_invoice import SalesInvoiceExportStrategy
from .quote import QuoteExportStrategy
from .receival_slip import ReceivalSlipExportStrategy
from .asset_transaction import AssetTransactionExportStrategy
from .delivery_order import DeliveryOrderExportStrategy
from .project_file import ProjectFileExportStrategy
from .quote_request import QuoteRequestExportStrategy
from .purchase_order import PurchaseOrderExportStrategy
from .commissionist_agent import CommissionistAgentExportStrategy


def get_strategy_map():
    """
    Returns a dict of ModelClass → ExportStrategyClass.
    """
    from configurator.models import Configuration, ReceivalSlip
    from production.models import ProductionItem
    from invoices.models import SalesInvoice
    from sales.models import CommissionistAgent, Quote, DeliveryOrder
    from assets.models import AssetTransaction
    from management.models import ProjectFile
    from purchasing.models import QuoteRequest, PurchaseOrder

    return {
        Configuration:    ConfigurationExportStrategy,
        ProductionItem:   ProductionItemExportStrategy,
        SalesInvoice:     SalesInvoiceExportStrategy,
        Quote:            QuoteExportStrategy,
        ReceivalSlip:     ReceivalSlipExportStrategy,
        AssetTransaction: AssetTransactionExportStrategy,
        DeliveryOrder:    DeliveryOrderExportStrategy,
        ProjectFile:      ProjectFileExportStrategy,
        QuoteRequest:     QuoteRequestExportStrategy,
        PurchaseOrder:    PurchaseOrderExportStrategy,
        CommissionistAgent: CommissionistAgentExportStrategy,
    }
