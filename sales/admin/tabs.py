from django.db.models import Count
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext as _

from core.admin.company_access import get_user_companies
from sales.models import DeliveryOrder, DeliveryOrderStatus

STATUS_PARAM = "status__exact"

# Work queues in the order an order moves through them, and whether the tab
# shows how many orders are waiting there.
_QUEUES = (
    (DeliveryOrderStatus.DRAFT, True),
    (DeliveryOrderStatus.PREPARED, True),
    (DeliveryOrderStatus.DISPATCHED, False),
    (DeliveryOrderStatus.DELIVERED, False),
    (DeliveryOrderStatus.CANCELLED, False),
)


def _title(label, count):
    if not count:
        return label
    return format_html(
        '{} <span class="ml-1.5 inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 '
        'rounded-full bg-primary-600 text-white text-[11px] font-bold leading-none">{}</span>',
        label, count,
    )


def _is_active(status):
    # Unfold's default match would light "All" (no query string) on every tab.
    def active(request):
        return request.GET.getlist(STATUS_PARAM) == ([status] if status else [])
    return active


def delivery_order_tabs(request: HttpRequest) -> list[dict]:
    """Delivery order changelist split by shipping status, with a count on
    the queues that still need someone to act (pending, prepared)."""
    changelist = reverse("admin:sales_deliveryorder_changelist")
    counted = [status for status, with_count in _QUEUES if with_count]
    counts = {}
    if request.user.has_perm("sales.view_deliveryorder"):
        counts = dict(
            DeliveryOrder.objects.filter(
                company__in=get_user_companies(request.user), status__in=counted,
            ).values_list("status").annotate(n=Count("pk")).order_by()
        )

    items = [
        {
            "title": _title(status.label, counts.get(status.value)),
            "link": f"{changelist}?{STATUS_PARAM}={status.value}",
            "active": _is_active(status.value),
        }
        for status, _with_count in _QUEUES
    ]
    items.append({"title": _("All"), "link": changelist, "active": _is_active(None)})
    for item in items:
        item["permission"] = lambda request: request.user.has_perm("sales.view_deliveryorder")

    return [{"models": ["sales.deliveryorder"], "items": items}]
