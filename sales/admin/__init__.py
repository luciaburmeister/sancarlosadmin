"""
Sales admin package — one module per document/entity so each admin stays
small and easy to find:

- client.py          → Client + address/contact/bank-account admins & inlines
- quote.py           → Quote, quote items, photos, configuration groups
- delivery_order.py  → Delivery orders (export + send email actions)
- delivery_order_return.py → Returns of goods from delivery orders + return reasons
- shipping_agency.py → Shipping agencies used by delivery orders
- discount_policy.py → Discount policies and rules
- commissionist_agent.py → Commissionist agents
- sale_zone.py       → Sale zones
- order.py           → Orders
- mixins.py          → Shared import/export helpers
"""

from .mixins import _CompanyImportMixin
from .sale_zone import SaleZoneAdmin
from .shipping_agency import ShippingAgencyAdmin
from .commissionist_agent import CommissionistAgentAdmin
from .discount_policy import DiscountRuleInline, DiscountPolicyAdmin
from .order import OrderAdmin
from .client import (
    ClientAdmin,
    ClientAddressAdmin,
    ClientBankAccountAdmin,
    ClientAddressInline,
    ClientContactInline,
    ClientBankAccountInline,
    ClientAppliedTaxInline,
)
from .quote import (
    QuoteAdmin,
    QuoteItemAdmin,
    QuoteInventoryItemAdmin,
    QuoteConfigurationGroupAdmin,
    QuotePhotoCategoryAdmin,
    QuotePhotoInline,
    QuoteItemInline,
    QuoteInventoryItemInline,
    QuoteConfigurationGroupInline,
    ConfigurationReadOnlyInline,
    QuoteInventoryItemAppliedTaxInline,
)
from .delivery_order import (
    DeliveryOrderAdmin,
    DeliveryOrderItemAdmin,
    DeliveryOrderItemInline,
    IdentityCodeDeliveryInline,
)
from .delivery_order_return import (
    DeliveryOrderReturnAdmin,
    DeliveryOrderReturnInline,
    ReturnReasonAdmin,
)
