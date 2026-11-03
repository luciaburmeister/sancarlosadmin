"""Values of the Shopify Admin API enums the integration acts on."""

from enum import StrEnum


class AccessScope(StrEnum):
    """Admin API access scopes."""

    READ_PRODUCTS = "read_products"
    WRITE_PRODUCTS = "write_products"
    READ_INVENTORY = "read_inventory"
    WRITE_INVENTORY = "write_inventory"
    READ_LOCATIONS = "read_locations"
    READ_ORDERS = "read_orders"
    READ_CUSTOMERS = "read_customers"
    WRITE_CUSTOMERS = "write_customers"
    WRITE_DISCOUNTS = "write_discounts"
    WRITE_DRAFT_ORDERS = "write_draft_orders"
    READ_MERCHANT_MANAGED_FULFILLMENT_ORDERS = "read_merchant_managed_fulfillment_orders"
    WRITE_MERCHANT_MANAGED_FULFILLMENT_ORDERS = "write_merchant_managed_fulfillment_orders"


class OrderFinancialStatus(StrEnum):
    """``Order.displayFinancialStatus``."""

    PENDING = "PENDING"
    AUTHORIZED = "AUTHORIZED"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REFUNDED = "REFUNDED"
    VOIDED = "VOIDED"
    EXPIRED = "EXPIRED"

    @classmethod
    def paid(cls) -> frozenset["OrderFinancialStatus"]:
        """Payment captured or guaranteed: the order is a sale and reserves stock."""
        return frozenset({cls.AUTHORIZED, cls.PARTIALLY_PAID, cls.PAID, cls.PARTIALLY_REFUNDED, cls.REFUNDED})

    @classmethod
    def captured(cls) -> frozenset["OrderFinancialStatus"]:
        """The money was taken: an issued invoice can be marked as paid."""
        return frozenset({cls.PAID, cls.PARTIALLY_REFUNDED, cls.REFUNDED})


class FulfillmentStatus(StrEnum):
    """``Fulfillment.status``."""

    SUCCESS = "SUCCESS"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"
    FAILURE = "FAILURE"
    OPEN = "OPEN"
    PENDING = "PENDING"


class FulfillmentOrderStatus(StrEnum):
    """``FulfillmentOrder.status``."""

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    SCHEDULED = "SCHEDULED"
    ON_HOLD = "ON_HOLD"
    INCOMPLETE = "INCOMPLETE"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"

    @classmethod
    def open(cls) -> frozenset["FulfillmentOrderStatus"]:
        """Fulfillment orders that can still ship."""
        return frozenset({cls.OPEN, cls.IN_PROGRESS})


class ProductStatus(StrEnum):
    """``Product.status``."""

    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"
    DRAFT = "DRAFT"
    UNLISTED = "UNLISTED"


class InventoryPolicy(StrEnum):
    """``ProductVariant.inventoryPolicy``: whether Shopify sells a variant without stock."""

    CONTINUE = "CONTINUE"
    DENY = "DENY"


class DraftOrderStatus(StrEnum):
    """``DraftOrder.status``."""

    OPEN = "OPEN"
    INVOICE_SENT = "INVOICE_SENT"
    COMPLETED = "COMPLETED"
