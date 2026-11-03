from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db.models import Field, Q, QuerySet
from django.forms import ModelChoiceField
from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import URLPattern, path, reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import action, display
from unfold.widgets import UnfoldAdminPasswordToggleWidget

from apps.shopify.client import ShopifyClient
from apps.shopify.exceptions import ShopifyError
from apps.shopify.models import (
    ClientMode,
    FulfillmentBy,
    FulfillmentCancellation,
    InvoiceTiming,
    RefundStatus,
    ShopifyApp,
    ShopifyCustomerLink,
    ShopifyFulfillment,
    ShopifyLocationMap,
    ShopifyOrder,
    ShopifyRefund,
    ShopifyStore,
    ShopifyVariantLink,
    ShopifyWebhookEvent,
    VariantLinkStatus,
    WebhookEventStatus,
)
from apps.shopify.services.catalog import ShopifyCatalogService
from apps.shopify.services.discounts import ShopifyDiscountService
from apps.shopify.services.fulfillments import ShopifyFulfillmentService
from apps.shopify.services.invoicing import ShopifyInvoicingService
from apps.shopify.services.oauth import ShopifyOAuthService
from apps.shopify.services.orders import ShopifyOrderService
from apps.shopify.services.stock import ShopifyStockService
from apps.shopify.services.webhooks import ShopifyWebhookService
from apps.shopify.views import GiveWebStoreAccessView
from core.admin.company_admin import CompanyRestrictedAdmin

logger = logging.getLogger(__name__)


class ShopifyAppForm(forms.ModelForm):
    client_secret = forms.CharField(
        required=False,
        widget=UnfoldAdminPasswordToggleWidget(render_value=False),
        label=_("Client secret"),
        help_text=_("From the app's settings in the Dev Dashboard. Leave blank to keep the stored secret."),
    )

    class Meta:
        model = ShopifyApp
        fields = ("name", "client_id")

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean()
        if not self.instance.client_secret_encrypted and not cleaned.get("client_secret", "").strip():
            self.add_error("client_secret", _("Enter the app's client secret."))
        return cleaned

    def save(self, commit: bool = True) -> ShopifyApp:
        instance = super().save(commit=False)
        secret = self.cleaned_data.get("client_secret", "").strip()
        if secret:
            instance.set_client_secret(secret)
        if commit:
            instance.save()
        return instance


@admin.register(ShopifyApp)
class ShopifyAppAdmin(ModelAdmin):
    form = ShopifyAppForm
    list_display = ("name", "client_id")
    search_fields = ("name", "client_id")


class ShopifyLocationInline(TabularInline):
    model = ShopifyLocationMap
    fields = ("name", "is_active", "warehouse")
    readonly_fields = ("name", "is_active")
    extra = 0
    can_delete = False
    tab = True
    verbose_name_plural = _("Locations and warehouses")

    def has_add_permission(self, request: HttpRequest, obj: ShopifyStore | None = None) -> bool:
        return False  # locations come from Shopify ("Sync catalog")

    def formfield_for_foreignkey(self, db_field: Field, request: HttpRequest, **kwargs: Any) -> ModelChoiceField | None:
        if db_field.name == "warehouse":
            from inventory.models import Warehouse

            # The change view itself is company-scoped; here only the store's
            # own warehouses are offered (the model re-checks on save).
            object_id = request.resolver_match.kwargs.get("object_id") if request.resolver_match else None
            company_id = ShopifyStore.objects.filter(pk=object_id).values_list("company_id", flat=True).first()
            kwargs["queryset"] = Warehouse.objects.filter(company_id=company_id, is_active=True)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(ShopifyStore)
class ShopifyStoreAdmin(CompanyRestrictedAdmin, ModelAdmin):
    list_display = ("name", "shop_domain", "connected", "is_active")
    list_filter = ("is_active", "company")
    search_fields = ("name", "shop_domain")
    actions_detail = [
        {"title": _("Connection"), "icon": "link", "items": ["connect_store", "test_connection", "sync_webhooks"]},
        {"title": _("Catalog and stock"), "icon": "inventory_2", "items": ["sync_catalog", "push_catalog", "push_stock"]},
        {"title": _("Clients"), "icon": "group", "items": ["give_web_store_access", "sync_discounts"]},
    ]
    inlines = [ShopifyLocationInline]

    fieldsets = (
        (_("Store"), {"fields": ("name", "company", "shop_domain", "app", "is_active")}),
        (_("Catalog and stock"), {
            "classes": ["tab"],
            "fields": (
                "sync_stock", "push_catalog", "quote_requests", "made_to_measure", "customer_quotes", "pay_quotes_online",
                "category_field",
                "group_variants",
                "last_catalog_push",
            ),
        }),
        (_("Orders"), {
            "classes": ["tab"],
            "fields": ("import_orders", "client_mode", "web_client", "shipping_item", "tax_type", "orders_synced_at"),
        }),
        (_("Shipping"), {"classes": ["tab"], "fields": ("fulfillment_by", "notify_customer")}),
        (_("Client discounts"), {"classes": ["tab"], "fields": ("sync_client_discounts",)}),
        (_("Invoicing"), {
            "classes": ["tab"],
            "fields": (
                "invoice_on", "issue_invoices", "email_invoices", "record_payments", "refund_handling",
                "invoicing_since",
            ),
        }),
        (_("Connection"), {
            "classes": ["tab"],
            "fields": ("connected", "connected_at", "scopes", "access_token_expires_at"),
        }),
    )
    readonly_fields = (
        "connected", "connected_at", "scopes", "access_token_expires_at", "orders_synced_at", "invoicing_since",
        "last_catalog_push",
    )
    # Settings that only matter for another choice stay out of sight.
    conditional_fields = {
        "web_client": f"client_mode == '{ClientMode.SINGLE_CLIENT}'",
        "notify_customer": f"fulfillment_by == '{FulfillmentBy.ERP}'",
        "issue_invoices": f"invoice_on != '{InvoiceTiming.MANUAL}'",
        "email_invoices": f"invoice_on != '{InvoiceTiming.MANUAL}' && issue_invoices",
        "record_payments": f"invoice_on != '{InvoiceTiming.MANUAL}'",
        "refund_handling": f"invoice_on != '{InvoiceTiming.MANUAL}'",
    }
    # Both admins are company-restricted, so the search only offers the user's companies.
    autocomplete_fields = ("web_client", "shipping_item")

    def get_readonly_fields(self, request: HttpRequest, obj: ShopifyStore | None = None) -> list[str]:
        readonly = list(super().get_readonly_fields(request, obj))
        # The tokens belong to this exact store and app: moving a connected
        # store would point them at the wrong data.
        if obj is not None and obj.is_connected:
            readonly += ["shop_domain", "company", "app"]
        return readonly

    @display(description=_("Connected"), boolean=True)
    def connected(self, obj: ShopifyStore) -> bool:
        return obj.is_connected

    @display(description=_("Last catalog push"))
    def last_catalog_push(self, obj: ShopifyStore) -> str:
        if obj.catalog_push_running:
            return _("Running since %(time)s…") % {"time": date_format(timezone.localtime(obj.catalog_push_started_at), "DATETIME_FORMAT")}
        if obj.catalog_pushed_at is None:
            return "–"
        when = date_format(timezone.localtime(obj.catalog_pushed_at), "DATETIME_FORMAT")
        result = obj.catalog_push_result
        if "failed" in result:
            return _("%(time)s: failed. %(reason)s") % {"time": when, "reason": result["failed"]}
        summary = _("%(time)s: %(created)s created as drafts, %(updated)s updated, %(unchanged)s already up to date.") % {
            "time": when, **{key: result.get(key, 0) for key in ("created", "updated", "unchanged")}
        }
        if result.get("archived") or result.get("restored"):
            summary += " " + _("%(archived)s archived as no longer sold online, %(restored)s restored.") % {
                "archived": result.get("archived", 0), "restored": result.get("restored", 0)
            }
        if result.get("removed"):
            summary += " " + ngettext(
                "%(count)s variant removed as no longer sold online.",
                "%(count)s variants removed as no longer sold online.",
                result["removed"],
            ) % {"count": result["removed"]}
        if not result.get("error_count"):
            return summary
        count = result["error_count"]
        errors = ngettext("%(count)s item could not be pushed:", "%(count)s items could not be pushed:", count) % {"count": count}
        return format_html(
            "{}<br>{}{}", summary, errors, format_html_join("", "<br>{}", ((error,) for error in result["errors"]))
        )

    def _scoped_store(self, request: HttpRequest, object_id: str) -> ShopifyStore:
        return get_object_or_404(self.get_queryset(request).select_related("app"), pk=object_id)

    @staticmethod
    def _store_page(store: ShopifyStore) -> HttpResponseRedirect:
        return redirect(reverse("admin:shopify_shopifystore_change", args=[store.pk]))

    @action(description=_("Connect to Shopify"), icon="link", permissions=["change"])
    def connect_store(self, request: HttpRequest, object_id: str) -> HttpResponseRedirect:
        store = self._scoped_store(request, object_id)
        try:
            return redirect(ShopifyOAuthService.begin(request.session, store))
        except ShopifyError as exc:
            messages.error(request, str(exc))
            return self._store_page(store)

    @action(description=_("Test connection"), icon="wifi", permissions=["view"])
    def test_connection(self, request: HttpRequest, object_id: str) -> HttpResponseRedirect:
        store = self._scoped_store(request, object_id)
        try:
            data = ShopifyClient(store).graphql("{ shop { name } }")
        except ShopifyError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, _("Connected to %(shop)s.") % {"shop": data["shop"]["name"]})
        return self._store_page(store)

    @action(description=_("Sync webhooks"), icon="sync", permissions=["change"])
    def sync_webhooks(self, request: HttpRequest, object_id: str) -> HttpResponseRedirect:
        store = self._scoped_store(request, object_id)
        try:
            created = ShopifyWebhookService.sync_subscriptions(store)
        except ShopifyError as exc:
            messages.error(request, str(exc))
        else:
            if created:
                messages.success(request, _("Subscribed to: %(topics)s.") % {"topics": ", ".join(created)})
            else:
                messages.info(request, _("Webhooks are already up to date."))
        return self._store_page(store)

    @action(description=_("Sync catalog"), icon="inventory_2", permissions=["change"])
    def sync_catalog(self, request: HttpRequest, object_id: str) -> HttpResponseRedirect:
        store = self._scoped_store(request, object_id)
        try:
            ShopifyCatalogService.sync_locations(store)
            counts = ShopifyCatalogService.sync_catalog(store)
        except ShopifyError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                _(
                    "Catalog synced: %(matched)s matched, %(not_in_erp)s not in the ERP, "
                    "%(no_sku)s without SKU, %(ambiguous)s with a repeated SKU."
                )
                % {
                    "matched": counts[VariantLinkStatus.MATCHED],
                    "not_in_erp": counts[VariantLinkStatus.NOT_IN_ERP],
                    "no_sku": counts[VariantLinkStatus.NO_SKU],
                    "ambiguous": counts[VariantLinkStatus.AMBIGUOUS],
                },
            )
        return self._store_page(store)

    @action(description=_("Push catalog"), icon="add_business", permissions=["change"])
    def push_catalog(self, request: HttpRequest, object_id: str) -> HttpResponseRedirect:
        store = self._scoped_store(request, object_id)
        if not store.push_catalog:
            messages.warning(request, _("Turn on \"Create products in Shopify\" for this store first."))
            return self._store_page(store)
        if store.catalog_push_running:
            messages.info(request, _("A catalog push for this store is already running."))
            return self._store_page(store)
        from apps.shopify.tasks import push_catalog_for_store

        store.start_catalog_push()
        try:
            push_catalog_for_store.delay(store.pk)
        except Exception:
            logger.exception("Could not queue the catalog push of Shopify store %s", store.pk)
            store.record_catalog_push(failure=str(_("The task queue is unavailable; the push did not start.")))
            messages.error(request, _("The catalog push could not be started: the task queue is unavailable."))
        else:
            messages.success(
                request,
                _("Catalog push started in the background. Its result appears under \"Catalog and stock\" once it finishes."),
            )
        return self._store_page(store)

    def get_urls(self) -> list[URLPattern]:
        return [
            path(
                "<int:pk>/client-prices/",
                self.admin_site.admin_view(GiveWebStoreAccessView.as_view(model_admin=self)),
                name="shopify_shopifystore_web_store_access_form",
            ),
            *super().get_urls(),
        ]

    @action(description=_("Give clients web store access"), icon="person_add", permissions=["change"])
    def give_web_store_access(self, request: HttpRequest, object_id: str) -> HttpResponseRedirect:
        store = self._scoped_store(request, object_id)
        return redirect(reverse("admin:shopify_shopifystore_web_store_access_form", args=[store.pk]))

    @action(description=_("Sync client discounts"), icon="sell", permissions=["change"])
    def sync_discounts(self, request: HttpRequest, object_id: str) -> HttpResponseRedirect:
        store = self._scoped_store(request, object_id)
        try:
            result = ShopifyDiscountService.sync(store)
        except ShopifyError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, ngettext(
                "Client discounts synced: %(count)s customer gets them.",
                "Client discounts synced: %(count)s customers get them.",
                result.offered,
            ) % {"count": result.offered})
            for error in result.errors:
                messages.warning(request, error)
        return self._store_page(store)

    @action(description=_("Push stock"), icon="publish", permissions=["change"])
    def push_stock(self, request: HttpRequest, object_id: str) -> HttpResponseRedirect:
        store = self._scoped_store(request, object_id)
        if not store.sync_stock:
            messages.warning(request, _("Turn on \"Sync stock to Shopify\" for this store first."))
        else:
            try:
                result = ShopifyStockService.push(store)
            except ShopifyError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    _("Stock pushed: %(updated)s updated, %(unchanged)s already correct, %(not_tracked)s not tracked in Shopify.")
                    % {"updated": result.updated, "unchanged": result.unchanged, "not_tracked": result.not_tracked},
                )
        return self._store_page(store)


class ShopifyRefundInline(TabularInline):
    model = ShopifyRefund
    fields = ("refunded_at", "amount", "status", "credit_note", "note")
    readonly_fields = fields
    extra = 0
    can_delete = False
    tab = True

    def has_add_permission(self, request: HttpRequest, obj: ShopifyOrder | None = None) -> bool:
        return False


class ShopifyFulfillmentInline(TabularInline):
    model = ShopifyFulfillment
    fields = ("delivery_order", "synced_at", "cancellation", "error")
    readonly_fields = fields
    extra = 0
    can_delete = False
    show_change_link = True
    tab = True
    verbose_name_plural = _("Shipments")

    def has_add_permission(self, request: HttpRequest, obj: ShopifyOrder | None = None) -> bool:
        return False


class NeedsAttentionFilter(admin.SimpleListFilter):
    title = _("Needs attention")
    parameter_name = "attention"

    def lookups(self, request: HttpRequest, model_admin: admin.ModelAdmin) -> list[tuple[str, str]]:
        return [("yes", _("Yes"))]

    def queryset(self, request: HttpRequest, queryset: QuerySet[ShopifyOrder]) -> QuerySet[ShopifyOrder]:
        if self.value() == "yes":
            return queryset.filter(
                Q(error__gt="") | Q(invoicing_error__gt="") | Q(totals_match=False) | Q(refunds__status=RefundStatus.REVIEW)
                | Q(fulfillments__cancellation=FulfillmentCancellation.REVIEW)
            ).distinct()
        return queryset


@admin.register(ShopifyOrder)
class ShopifyOrderAdmin(CompanyRestrictedAdmin, ModelAdmin):
    company_field_name = "store__company"
    list_display = ("name", "store", "placed_at", "expediente", "quote", "invoice", "financial_status", "is_cancelled", "totals_match", "shopify_total", "refunded_total", "synced_at")
    list_filter = (NeedsAttentionFilter, "totals_match", "is_cancelled", "financial_status")
    search_fields = ("name", "order_id", "quote__project_name")
    actions = ["sync_again", "send_shipments", "invoice_again"]
    inlines = [ShopifyFulfillmentInline, ShopifyRefundInline]
    fieldsets = (
        (None, {"fields": (
            "store", "name", "order_id", "placed_at", "expediente", "quote", "invoice", "shipping_address",
            "financial_status", "is_cancelled",
        )}),
        (_("Totals"), {"fields": ("currency", "shopify_total", "erp_total", "totals_match", "refunded_total")}),
        (_("Synchronisation"), {"fields": ("error", "invoicing_error", "shopify_updated_at", "synced_at")}),
    )
    readonly_fields = (
        "store", "name", "order_id", "placed_at", "expediente", "quote", "invoice", "shipping_address", "financial_status",
        "is_cancelled", "shopify_updated_at", "currency", "shopify_total", "erp_total", "totals_match", "refunded_total",
        "error", "invoicing_error", "synced_at",
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet[ShopifyOrder]:
        return super().get_queryset(request).select_related("store", "quote__project_file", "invoice")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    @display(description=_("Expediente"))
    def expediente(self, obj: ShopifyOrder) -> str:
        project_file = obj.quote.project_file if obj.quote else None
        if project_file is None:
            return "–"
        return format_html(
            '<a href="{}">{}</a>', reverse("admin:management_projectfile_change", args=[project_file.pk]),
            project_file.reference or project_file.name,
        )

    @action(description=_("Sync again"), permissions=["change"])
    def sync_again(self, request: HttpRequest, queryset: QuerySet[ShopifyOrder]) -> None:
        failed = 0
        for record in queryset.select_related("store"):
            try:
                ShopifyOrderService.sync_order(record.store, record.order_id)
            except Exception as exc:
                ShopifyOrderService.record_failure(record.store, record.order_id, exc)
                failed += 1
        if failed:
            messages.warning(request, ngettext("%(count)s order could not be imported.", "%(count)s orders could not be imported.", failed) % {"count": failed})
        else:
            messages.success(request, _("Orders synced."))

    @action(description=_("Send shipments to Shopify"), permissions=["change"])
    def send_shipments(self, request: HttpRequest, queryset: QuerySet[ShopifyOrder]) -> None:
        """Push the dispatched delivery orders of these orders that Shopify doesn't have yet."""
        sent = failed = 0
        for store in ShopifyStore.objects.filter(pk__in=queryset.values("store")):
            pushed, not_pushed = ShopifyFulfillmentService.push_pending(store, orders=queryset.filter(store=store))
            sent, failed = sent + pushed, failed + not_pushed
        if failed:
            messages.warning(request, ngettext("%(count)s shipment could not be sent; see Shopify fulfillments.", "%(count)s shipments could not be sent; see Shopify fulfillments.", failed) % {"count": failed})
        messages.info(request, ngettext("%(count)s shipment sent to Shopify.", "%(count)s shipments sent to Shopify.", sent) % {"count": sent})

    @action(description=_("Invoice again"), permissions=["change"])
    def invoice_again(self, request: HttpRequest, queryset: QuerySet[ShopifyOrder]) -> None:
        """Retry invoicing and the refunds left for review, e.g. after fixing what stopped them."""
        ShopifyRefund.objects.filter(order__in=queryset, status=RefundStatus.REVIEW).update(status=RefundStatus.PENDING)
        for order_id in queryset.values_list("pk", flat=True):
            ShopifyInvoicingService.apply_locked(order_id)
        failed = queryset.filter(Q(invoicing_error__gt="") | Q(refunds__status=RefundStatus.REVIEW)).distinct().count()
        if failed:
            messages.warning(request, ngettext("%(count)s order still needs attention.", "%(count)s orders still need attention.", failed) % {"count": failed})
        else:
            messages.success(request, _("Orders invoiced."))


@admin.register(ShopifyFulfillment)
class ShopifyFulfillmentAdmin(CompanyRestrictedAdmin, ModelAdmin):
    company_field_name = "store__company"
    list_display = ("order", "delivery_order", "fulfillment_id", "cancellation", "error", "synced_at")
    list_filter = ("cancellation",)
    search_fields = ("fulfillment_id", "order__name", "delivery_order__number")
    readonly_fields = (
        "store", "order", "delivery_order", "fulfillment_id", "cancellation", "cancelled_at", "error", "synced_at",
    )
    actions = ["goods_returned", "goods_lost"]
    actions_detail = ["goods_returned_detail", "goods_lost_detail"]

    def get_queryset(self, request: HttpRequest) -> QuerySet[ShopifyFulfillment]:
        return super().get_queryset(request).select_related("store", "order", "delivery_order")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_resolve_permission(self, request: HttpRequest, object_id: str | None = None) -> bool:
        # Undoing a dispatch moves stock: it needs the delivery order permission too.
        return self.has_change_permission(request) and request.user.has_perm("sales.change_deliveryorder")

    @action(description=_("Goods are back in the warehouse"), permissions=["resolve"])
    def goods_returned(self, request: HttpRequest, queryset: QuerySet[ShopifyFulfillment]) -> None:
        self._resolve(request, queryset, ShopifyFulfillmentService.goods_returned)

    @action(description=_("Goods were lost"), permissions=["resolve"])
    def goods_lost(self, request: HttpRequest, queryset: QuerySet[ShopifyFulfillment]) -> None:
        self._resolve(request, queryset, ShopifyFulfillmentService.goods_lost)

    @action(description=_("Goods are back in the warehouse"), icon="inventory", permissions=["resolve"])
    def goods_returned_detail(self, request: HttpRequest, object_id: str) -> HttpResponseRedirect:
        return self._resolve_one(request, object_id, ShopifyFulfillmentService.goods_returned)

    @action(description=_("Goods were lost"), icon="report", permissions=["resolve"])
    def goods_lost_detail(self, request: HttpRequest, object_id: str) -> HttpResponseRedirect:
        return self._resolve_one(request, object_id, ShopifyFulfillmentService.goods_lost)

    def _resolve_one(
        self, request: HttpRequest, object_id: str, resolve: Callable[[ShopifyFulfillment], object]
    ) -> HttpResponseRedirect:
        self._resolve(request, self.get_queryset(request).filter(pk=object_id), resolve)
        return redirect(reverse("admin:shopify_shopifyfulfillment_change", args=[object_id]))

    def _resolve(
        self, request: HttpRequest, queryset: QuerySet[ShopifyFulfillment], resolve: Callable[[ShopifyFulfillment], object]
    ) -> None:
        done = 0
        for fulfillment in queryset.filter(cancellation=FulfillmentCancellation.REVIEW):
            try:
                resolve(fulfillment)
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
            else:
                done += 1
        if done:
            messages.success(request, ngettext("%(count)s shipment resolved.", "%(count)s shipments resolved.", done) % {"count": done})
        elif not queryset.filter(cancellation=FulfillmentCancellation.REVIEW).exists():
            messages.warning(request, _("Only shipments cancelled in Shopify and waiting for review can be resolved."))


@admin.register(ShopifyCustomerLink)
class ShopifyCustomerLinkAdmin(CompanyRestrictedAdmin, ModelAdmin):
    company_field_name = "store__company"
    list_display = ("client", "store", "web_store_access", "discount_profile", "discount_error")
    # Only the stores of the links the user may see, not every company's.
    list_filter = ("web_store_access", ("store", admin.RelatedOnlyFieldListFilter))
    search_fields = ("client__name", "client__email", "customer_id")
    readonly_fields = ("store", "client", "customer_id", "web_store_access", "discount_profile", "discount_error")
    actions = ["give_web_store_access", "remove_web_store_access"]

    def get_queryset(self, request: HttpRequest) -> QuerySet[ShopifyCustomerLink]:
        return super().get_queryset(request).select_related("store", "client", "discount_profile")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False  # given from the store ("Give clients web store access") or by web orders

    @action(description=_("Give web store access"), permissions=["change"])
    def give_web_store_access(self, request: HttpRequest, queryset: QuerySet[ShopifyCustomerLink]) -> None:
        """For customers already linked, e.g. by their web orders."""
        given = ShopifyDiscountService.set_web_store_access(queryset, True)
        messages.success(request, ngettext(
            "%(count)s customer gets web store access.", "%(count)s customers get web store access.", given
        ) % {"count": given})

    @action(description=_("Remove web store access"), permissions=["change"])
    def remove_web_store_access(self, request: HttpRequest, queryset: QuerySet[ShopifyCustomerLink]) -> None:
        removed = ShopifyDiscountService.set_web_store_access(queryset, False)
        messages.success(request, ngettext(
            "%(count)s customer no longer has web store access.", "%(count)s customers no longer have web store access.",
            removed,
        ) % {"count": removed})


@admin.register(ShopifyVariantLink)
class ShopifyVariantLinkAdmin(CompanyRestrictedAdmin, ModelAdmin):
    company_field_name = "store__company"
    list_display = ("product_title", "variant_title", "sku", "status", "item", "store")
    list_filter = ("status",)
    search_fields = ("sku", "product_title", "variant_title", "item__code", "item__name")
    readonly_fields = (
        "store", "product_title", "variant_title", "sku", "status", "item",
        "product_id", "variant_id", "inventory_item_id", "synced_at",
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet[ShopifyVariantLink]:
        return super().get_queryset(request).select_related("store", "item")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False


@admin.register(ShopifyWebhookEvent)
class ShopifyWebhookEventAdmin(CompanyRestrictedAdmin, ModelAdmin):
    company_field_name = "store__company"
    list_display = ("topic", "store", "status", "attempts", "received_at", "processed_at")
    list_filter = ("status", "topic")
    search_fields = ("topic", "webhook_id", "event_id")
    actions = ["process_again"]
    readonly_fields = (
        "store", "topic", "status", "attempts", "error", "webhook_id", "event_id",
        "api_version", "triggered_at", "received_at", "processed_at", "payload",
    )

    def get_queryset(self, request: HttpRequest) -> QuerySet[ShopifyWebhookEvent]:
        return super().get_queryset(request).select_related("store")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    @action(description=_("Process again"), permissions=["change"])
    def process_again(self, request: HttpRequest, queryset: QuerySet[ShopifyWebhookEvent]) -> None:
        # Processed events are final; ignored ones run if their topic now has a handler.
        retried = 0
        for event in queryset.exclude(status=WebhookEventStatus.PROCESSED):
            ShopifyWebhookService.process(event.pk)
            retried += 1
        messages.info(
            request,
            ngettext("Processed %(count)s event.", "Processed %(count)s events.", retried) % {"count": retried},
        )
