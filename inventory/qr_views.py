"""
Inventory QR code view.

One view handles all three movement types (receive / transfer / adjust).
The QR payload is the absolute URL of the already-existing movement form,
registered inside InventoryItemAdmin.get_urls():

    /admin/inventory/inventoryitem/<pk>/receive/
    /admin/inventory/inventoryitem/<pk>/transfer/
    /admin/inventory/inventoryitem/<pk>/adjust/

URL pattern (registered three times in admin.py get_urls):
    path("<int:object_id>/qr/receive/",  ..., name="inventory_item_qr_receive")
    path("<int:object_id>/qr/transfer/", ..., name="inventory_item_qr_transfer")
    path("<int:object_id>/qr/adjust/",   ..., name="inventory_item_qr_adjust")
"""

import io

import qrcode
import qrcode.constants
from django.http import HttpResponse, Http404
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_GET

from inventory.models import InventoryItem

# Maps movement slug → admin movement URL name
_MOVEMENT_URL = {
    "receive":  "admin:inventory_item_receive",
    "transfer": "admin:inventory_item_transfer",
    "adjust":   "admin:inventory_item_adjust",
}


@require_GET
def item_qr_view(request, object_id: int, movement: str):
    """
    GET /admin/inventory/inventoryitem/<pk>/qr/<movement>/
    movement ∈ { receive, transfer, adjust }

    Returns a PNG QR code whose payload is the absolute URL of the
    corresponding movement form for this item.
    """
    if movement not in _MOVEMENT_URL:
        raise Http404(f"Unknown movement type: {movement!r}")

    get_object_or_404(InventoryItem, pk=object_id)   # 404 guard

    target_url = request.build_absolute_uri(
        reverse(_MOVEMENT_URL[movement], args=[object_id])
    )

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(target_url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)

    return HttpResponse(buffer.getvalue(), content_type="image/png")