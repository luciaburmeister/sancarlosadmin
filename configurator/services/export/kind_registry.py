"""
Shared registry mapping ExportTemplateKind → model class and product filter field.

Used by both the admin `test_object_selector` and the preview view
`_get_preview_object` to avoid duplicating the kind→model mapping.
"""


def _get_kind_model_map():
    """
    Returns a dict of kind_value → (ModelClass, product_filter_field_or_None).

    Lazy imports are used to avoid circular imports at module level.
    """
    from configurator.models import Configuration, ReceivalSlip
    from production.models import ProductionItem
    from invoices.models import SalesInvoice
    from sales.models import Quote, DeliveryOrder
    from assets.models import AssetTransaction
    from management.models import ProjectFile
    from purchasing.models import QuoteRequest, PurchaseOrder

    return {
        "quotation":              (Configuration,   "product"),
        "production_order":       (Configuration,   "product"),
        "production_ticket":      (ProductionItem,  None),
        "sales_invoice":          (SalesInvoice,    "configuration__product"),
        "consolidated_quotation": (Quote,           None),
        "receival_slip":          (ReceivalSlip,    "configuration__product"),
        "asset_transaction":      (AssetTransaction, None),
        "delivery_order":         (DeliveryOrder,   "configuration__product"),
        "repair_order":           (ProjectFile,     None),
        "quote_request":          (QuoteRequest,    None),
        "purchase_order":         (PurchaseOrder,   None),
    }


def get_queryset_for_kind(kind: str, *, product=None, company=None):
    """
    Returns a filtered QuerySet for the given export template kind.

    Args:
        kind: The ExportTemplateKind value (string).
        product: Optional product instance to filter by (using the kind's product field).
        company: Optional company instance to filter by.

    Returns:
        A QuerySet or empty list if the kind is unknown.
    """
    entry = _get_kind_model_map().get(kind)
    if not entry:
        return []

    model_cls, product_field = entry
    qs = model_cls.objects.all()

    if product and product_field:
        qs = qs.filter(**{product_field: product})

    if company and hasattr(qs, "filter"):
        qs = qs.filter(company=company)

    return qs


def get_preview_object(kind: str, *, product_id=None, company_id=None, object_id=None):
    """
    Returns a single model instance suitable for previewing an export template.

    If object_id is given, fetches that exact object.
    Otherwise returns the latest matching object (with fallback to any object of that kind).

    The production_ticket kind has special handling because its product
    relationship is indirect (through ticket → source_object → product).
    """
    entry = _get_kind_model_map().get(kind)
    if not entry:
        return None

    model_cls, product_field = entry

    # Direct lookup by ID
    if object_id:
        return model_cls.objects.filter(id=object_id).first()

    # Build filtered queryset
    filters = {}
    if company_id:
        filters["company_id"] = company_id

    qs = model_cls.objects.filter(**filters)

    # Product filtering
    if product_id and product_field:
        qs = qs.filter(**{f"{product_field}_id": product_id})
    elif product_id and kind == "production_ticket":
        # Special case: product relationship is indirect
        for item in qs.order_by("-id")[:50]:
            product = (
                getattr(item.ticket.source_object, "product", None)
                if getattr(item, "ticket", None)
                else None
            )
            if product and str(product.id) == str(product_id):
                return item

    result = qs.order_by("-id").first()
    if result:
        return result

    # Fallback: return the latest object of this type regardless of filters
    return model_cls.objects.order_by("-id").first()
