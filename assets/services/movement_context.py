def get_related_receipt(transaction):
    """
    Most relevant receipt for this transaction: filed directly on it, else
    filed elsewhere pointing back to it as the original movement.
    """
    receipt = transaction.receipts.order_by("-created_at").first()
    if receipt:
        return receipt
    return transaction.receipt_links.order_by("-created_at").first()


def get_related_movement(transaction):
    """The movement transaction that this transaction's own receipt points back to, if any."""
    receipt = transaction.receipts.order_by("-created_at").first()
    if receipt and receipt.movement_transaction_id and receipt.movement_transaction_id != transaction.pk:
        return receipt.movement_transaction
    return None


def build_asset_transaction_context(transaction):
    """
    Pricing/applicability context builder for AssetTransaction, registered with
    WorkEntryService. Adds receipt and related-movement data on top of the
    transaction's own fields, so pricing rules can reference e.g.
    `receipt.peso_neto` (movement -> its later receipt) or
    `movement.from_zone` (receipt -> the movement it was filed against).
    """
    from management.services.work_entry import WorkEntryService

    context = WorkEntryService._auto_extract_context(transaction)

    receipt = get_related_receipt(transaction)
    if receipt:
        context["receipt"] = {"nro_albaran": receipt.nro_albaran, **receipt.data}

    movement = get_related_movement(transaction)
    if movement:
        context["movement"] = WorkEntryService._auto_extract_context(movement)

    return context
