from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from timestamps.models import Timestampable

from company.models import Company
from core.encryption import SecretCipher

if TYPE_CHECKING:
    from apps.shopify.services.catalog import CatalogPushResult
    from inventory.models import InventoryItem


class ShopifyApp(Timestampable):
    """
    A Shopify app registered in the Dev Dashboard, through which stores connect.

    Apps with custom distribution install on one store, or on several stores of
    one Shopify Plus organization, so the pilot uses one app per store and a
    later Plus setup can point many stores at a single app. Shopify signs OAuth
    callbacks and webhooks with the app's client secret.
    """

    name = models.CharField(max_length=150, verbose_name=_("Name"))
    client_id = models.CharField(max_length=255, unique=True, verbose_name=_("Client ID"))
    client_secret_encrypted = models.TextField(blank=True, editable=False)

    class Meta:
        verbose_name = _("Shopify app")
        verbose_name_plural = _("Shopify apps")
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def set_client_secret(self, secret: str) -> None:
        self.client_secret_encrypted = SecretCipher.encrypt(secret)

    def get_client_secret(self) -> str:
        return SecretCipher.decrypt(self.client_secret_encrypted) if self.client_secret_encrypted else ""


class ClientMode(models.TextChoices):
    PER_CUSTOMER = "per_customer", _("One client per customer")
    SINGLE_CLIENT = "single_client", _("One client for all web orders")


class FulfillmentBy(models.TextChoices):
    ERP = "erp", _("ERP warehouse ships")
    SHOPIFY = "shopify", _("Shopify or a partner ships")


class QuoteRequestMode(models.TextChoices):
    OFF = "off", _("Off")
    ALONGSIDE = "alongside", _("Alongside buying")
    INSTEAD = "instead", _("Instead of buying")


class InvoiceTiming(models.TextChoices):
    MANUAL = "manual", _("By hand")
    PAYMENT = "payment", _("When the order is paid")
    SHIPMENT = "shipment", _("When each shipment is dispatched")


class RefundHandling(models.TextChoices):
    REVIEW = "review", _("Review by hand")
    CREDIT_NOTE = "credit_note", _("Create a credit note")


class ShopifyStoreQuerySet(models.QuerySet):
    def syncing_stock(self) -> ShopifyStoreQuerySet:
        """Active, connected stores whose stock the ERP keeps up to date."""
        return self.filter(is_active=True, sync_stock=True).exclude(access_token_encrypted="")

    def pushing_catalog(self) -> ShopifyStoreQuerySet:
        """Active, connected stores that create and update products from the ERP."""
        return self.filter(is_active=True, push_catalog=True).exclude(access_token_encrypted="")

    def importing_orders(self) -> ShopifyStoreQuerySet:
        """Active, connected stores whose orders become ERP quotes."""
        return self.filter(is_active=True, import_orders=True).exclude(access_token_encrypted="")


class ShopifyStore(Timestampable):
    """
    One Shopify store connected to the ERP.

    The store belongs to exactly one company: every order, product and
    webhook coming from it is processed for that company and no other.
    Access tokens are obtained through the app's OAuth install flow and
    stored encrypted.
    """

    # Shopify's permanent store address, e.g. "my-store.myshopify.com". Custom
    # domains change; this one never does, so it identifies the store.
    SHOP_DOMAIN_RE = r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$"
    CATALOG_PUSH_STALE_AFTER = timedelta(hours=1)
    CATALOG_PUSH_ERRORS_KEPT = 20

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="shopify_stores",
        verbose_name=_("Company"),
    )
    app = models.ForeignKey(
        ShopifyApp,
        on_delete=models.PROTECT,
        null=True,
        blank=False,
        related_name="stores",
        verbose_name=_("Shopify app"),
        help_text=_("The app this store installs to connect with the ERP."),
    )
    name = models.CharField(
        max_length=150,
        verbose_name=_("Name"),
        help_text=_("Internal label, e.g. 'Fistex – Online store'."),
    )
    shop_domain = models.CharField(
        max_length=255,
        unique=True,
        validators=[
            RegexValidator(
                SHOP_DOMAIN_RE,
                message=_("Enter the store's myshopify.com address, e.g. my-store.myshopify.com."),
            )
        ],
        verbose_name=_("Shop domain"),
        help_text=_("The store's permanent myshopify.com address, not a custom domain."),
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name=_("Active"),
        help_text=_("Inactive stores are not synchronised and their webhooks are ignored."),
    )
    sync_stock = models.BooleanField(
        default=False,
        verbose_name=_("Sync stock to Shopify"),
        help_text=_(
            "Keep Shopify's stock equal to the ERP's: each linked product shows its mapped "
            "warehouse's stock minus reservations. Shopify's own stock edits are overwritten."
        ),
    )

    push_catalog = models.BooleanField(
        default=False,
        verbose_name=_("Create products in Shopify"),
        help_text=_(
            "Items marked 'Sell online' are created in Shopify as draft products, and their price, "
            "cost and SKU are kept up to date from the ERP, with the item's tax added when the "
            "store's prices include tax. Titles, descriptions and photos are edited in Shopify. "
            "Unticking 'Sell online' archives the product."
        ),
    )

    quote_requests = models.CharField(
        max_length=20,
        choices=QuoteRequestMode.choices,
        default=QuoteRequestMode.OFF,
        verbose_name=_("Quote requests"),
        help_text=_(
            "Alongside buying: each item's 'Sold online as' decides whether customers buy it, request "
            "a quote or both. Instead of buying: customers can only request quotes. Requests arrive "
            "as draft quotes."
        ),
    )

    catalog_push_started_at = models.DateTimeField(null=True, blank=True, editable=False)
    catalog_pushed_at = models.DateTimeField(null=True, blank=True, editable=False)
    # {"created", "updated", "unchanged", "archived", "restored", "errors": [first ones], "error_count"} or {"failed": reason}
    catalog_push_result = models.JSONField(default=dict, blank=True, editable=False)

    import_orders = models.BooleanField(
        default=False,
        verbose_name=_("Import orders"),
        help_text=_(
            "Turn Shopify orders into ERP quotes: sent while unpaid, accepted (reserving "
            "stock) once paid, rejected when cancelled. Orders from before this is turned "
            "on are not imported."
        ),
    )
    client_mode = models.CharField(
        max_length=20,
        choices=ClientMode.choices,
        default=ClientMode.PER_CUSTOMER,
        verbose_name=_("Clients for web orders"),
    )
    web_client = models.ForeignKey(
        "sales.Client",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Web client"),
        help_text=_("Client that receives every web order when using one client for all web orders."),
    )
    shipping_item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Shipping item"),
        help_text=_("Service item for the shipping a customer pays, e.g. 'Portes'."),
    )
    tax_type = models.ForeignKey(
        "invoices.TaxType",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Tax type"),
        help_text=_("Tax that Shopify's tax rates are recorded as on quotes, e.g. IVA."),
    )
    fulfillment_by = models.CharField(
        max_length=20,
        choices=FulfillmentBy.choices,
        default=FulfillmentBy.ERP,
        verbose_name=_("Who ships web orders"),
        help_text=_(
            "ERP warehouse: paid orders get a delivery order pending shipment, and dispatching it "
            "sends the tracking to Shopify. Shopify or a partner: each shipment made in Shopify "
            "creates a dispatched delivery order in the ERP, taking the stock out."
        ),
    )
    notify_customer = models.BooleanField(
        default=True,
        verbose_name=_("Email the customer when shipped"),
        help_text=_("Shopify sends its shipping confirmation with the tracking link when the ERP dispatches an order."),
    )
    orders_synced_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("Orders checked until"),
        help_text=_("Orders updated after this are fetched by the periodic check, in case a webhook was missed."),
    )
    invoice_on = models.CharField(
        max_length=20,
        choices=InvoiceTiming.choices,
        default=InvoiceTiming.MANUAL,
        verbose_name=_("Invoice web orders"),
        help_text=_(
            "By hand: staff invoice the delivery orders as usual. When paid: the whole order is "
            "invoiced at once. When dispatched: each shipment gets its own invoice."
        ),
    )
    issue_invoices = models.BooleanField(
        default=False,
        verbose_name=_("Issue invoices automatically"),
        help_text=_(
            "Off: invoices wait as pro forma for review. On: they are numbered and sent to the "
            "tax agency, as when finalising one by hand."
        ),
    )
    email_invoices = models.BooleanField(
        default=False,
        verbose_name=_("Email invoices to the customer"),
        help_text=_("Sent from the company's invoice email once issued."),
    )
    record_payments = models.BooleanField(
        default=True,
        verbose_name=_("Record Shopify payments"),
        help_text=_("Issued invoices of orders paid in Shopify are marked as paid by card."),
    )
    refund_handling = models.CharField(
        max_length=20,
        choices=RefundHandling.choices,
        default=RefundHandling.REVIEW,
        verbose_name=_("Refunds of invoiced goods"),
        help_text=_(
            "Review by hand: refunds are listed for staff to settle. Credit note: one is created "
            "for the refunded products and shipping, issued like the invoices."
        ),
    )
    invoicing_since = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        verbose_name=_("Invoicing orders placed since"),
        help_text=_("Older orders are invoiced by hand, so nothing is billed twice."),
    )

    access_token_encrypted = models.TextField(blank=True, editable=False)
    access_token_expires_at = models.DateTimeField(
        null=True, blank=True, editable=False, verbose_name=_("Access token expires at")
    )
    refresh_token_encrypted = models.TextField(blank=True, editable=False)
    refresh_token_expires_at = models.DateTimeField(
        null=True, blank=True, editable=False, verbose_name=_("Refresh token expires at")
    )
    scopes = models.CharField(
        max_length=1000,
        blank=True,
        editable=False,
        verbose_name=_("Granted scopes"),
        help_text=_("Access the store granted when the app was installed."),
    )
    connected_at = models.DateTimeField(
        null=True, blank=True, editable=False, verbose_name=_("Connected at")
    )

    objects = ShopifyStoreQuerySet.as_manager()

    class Meta:
        verbose_name = _("Shopify store")
        verbose_name_plural = _("Shopify stores")
        ordering = ["company", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.shop_domain})"

    @staticmethod
    def normalize_shop_domain(value: str) -> str:
        """Lowercase and strip a pasted URL down to ``name.myshopify.com``."""
        value = (value or "").strip().lower()
        value = re.sub(r"^https?://", "", value)
        return value.split("/", 1)[0]

    def clean_fields(self, exclude: Iterable[str] | None = None) -> None:
        # Normalise before the field validators run, so a pasted URL passes.
        self.shop_domain = self.normalize_shop_domain(self.shop_domain)
        super().clean_fields(exclude=exclude)

    def clean(self) -> None:
        super().clean()
        errors = {}
        for field in ("web_client", "shipping_item"):
            related = getattr(self, field)
            if related is not None and self.company_id and related.company_id != self.company_id:
                errors[field] = _("Choose one of the store's company.")
        if self.import_orders:
            if self.tax_type_id is None:
                errors["tax_type"] = _("Choose the tax type to import orders.")
            if self.client_mode == ClientMode.SINGLE_CLIENT and self.web_client_id is None:
                errors["web_client"] = _("Choose the client for web orders.")
        if errors:
            raise ValidationError(errors)

    def save(self, *args: Any, **kwargs: Any) -> None:
        changed = []
        # Importing starts now: the periodic check doesn't reach back to older orders.
        if self.import_orders and self.orders_synced_at is None:
            self.orders_synced_at = timezone.now()
            changed.append("orders_synced_at")
        # So does automatic invoicing; turning it off and on starts again.
        invoicing = self.invoice_on != InvoiceTiming.MANUAL
        if invoicing != (self.invoicing_since is not None):
            self.invoicing_since = timezone.now() if invoicing else None
            changed.append("invoicing_since")
        if changed and kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = {*kwargs["update_fields"], *changed}
        super().save(*args, **kwargs)

        from apps.shopify.services.push_scheduler import ShopifyPushScheduler  # imports this module

        ShopifyPushScheduler.forget_store_settings()

    def sale_mode(self, item: InventoryItem) -> str:
        """How the store sells the item: an ``inventory.choices.OnlineSaleMode`` value."""
        from inventory.choices import OnlineSaleMode

        if self.quote_requests == QuoteRequestMode.INSTEAD:
            return OnlineSaleMode.QUOTE
        if self.quote_requests == QuoteRequestMode.ALONGSIDE:
            return item.online_sale_mode
        return OnlineSaleMode.BUY

    @property
    def catalog_push_running(self) -> bool:
        """A full catalog push was started and hasn't finished; one stuck for an hour no longer counts."""
        started = self.catalog_push_started_at
        if started is None or (self.catalog_pushed_at and self.catalog_pushed_at >= started):
            return False
        return timezone.now() - started < self.CATALOG_PUSH_STALE_AFTER

    def start_catalog_push(self) -> None:
        self.catalog_push_started_at = timezone.now()
        self.save(update_fields=["catalog_push_started_at", "updated_at"])

    def record_catalog_push(self, result: CatalogPushResult | None = None, failure: str = "") -> None:
        """Keep how the last full catalog push went, for the store page. Saves."""
        if failure:
            self.catalog_push_result = {"failed": failure[:2000]}
        else:
            self.catalog_push_result = {
                "created": result.created,
                "updated": result.updated,
                "unchanged": result.unchanged,
                "archived": result.archived,
                "restored": result.restored,
                "errors": result.errors[:self.CATALOG_PUSH_ERRORS_KEPT],
                "error_count": len(result.errors),
            }
        self.catalog_pushed_at = timezone.now()
        self.save(update_fields=["catalog_push_result", "catalog_pushed_at", "updated_at"])

    def invoices_automatically(self, order: ShopifyOrder) -> bool:
        """Whether the store invoices this order itself, rather than staff by hand."""
        return bool(
            self.invoicing_since and order.placed_at and order.placed_at >= self.invoicing_since
        )

    @property
    def is_connected(self) -> bool:
        return bool(self.access_token_encrypted)

    def set_tokens(
        self,
        *,
        access_token: str,
        access_token_expires_at: datetime | None,
        refresh_token: str,
        refresh_token_expires_at: datetime | None,
    ) -> None:
        """Store a token pair from Shopify, encrypted. The caller saves."""
        if not access_token:
            raise ValidationError(_("Shopify returned no access token."))
        self.access_token_encrypted = SecretCipher.encrypt(access_token)
        self.access_token_expires_at = access_token_expires_at
        self.refresh_token_encrypted = SecretCipher.encrypt(refresh_token) if refresh_token else ""
        self.refresh_token_expires_at = refresh_token_expires_at

    def get_access_token(self) -> str:
        return SecretCipher.decrypt(self.access_token_encrypted) if self.access_token_encrypted else ""

    def get_refresh_token(self) -> str:
        return SecretCipher.decrypt(self.refresh_token_encrypted) if self.refresh_token_encrypted else ""

    def has_scope(self, *scopes: str) -> bool:
        """Whether the store granted any of ``scopes`` when it connected."""
        granted = {scope.strip() for scope in self.scopes.split(",")}
        return not granted.isdisjoint(scopes)

    def disconnect(self) -> None:
        """Forget the store's tokens, e.g. after the app was uninstalled. Saves."""
        self.access_token_encrypted = ""
        self.access_token_expires_at = None
        self.refresh_token_encrypted = ""
        self.refresh_token_expires_at = None
        self.scopes = ""
        self.save(update_fields=[
            "access_token_encrypted", "access_token_expires_at",
            "refresh_token_encrypted", "refresh_token_expires_at", "scopes", "updated_at",
        ])


class WebhookEventStatus(models.TextChoices):
    RECEIVED = "received", _("Received")
    PROCESSED = "processed", _("Processed")
    IGNORED = "ignored", _("Ignored")
    FAILED = "failed", _("Failed")


class ShopifyWebhookEvent(models.Model):
    """
    One webhook delivery from a store, saved before it is processed.

    Saving first means a delivery is never lost if processing fails, and the
    unique ``webhook_id`` (Shopify's ``X-Shopify-Webhook-Id``) makes a repeated
    delivery a no-op. Shopify does not guarantee delivery order, so handlers
    must not rely on it.
    """

    store = models.ForeignKey(
        ShopifyStore,
        on_delete=models.CASCADE,
        related_name="webhook_events",
        verbose_name=_("Shopify store"),
    )
    webhook_id = models.CharField(max_length=255, unique=True, verbose_name=_("Webhook ID"))
    event_id = models.CharField(max_length=255, blank=True, db_index=True, verbose_name=_("Event ID"))
    topic = models.CharField(max_length=100, db_index=True, verbose_name=_("Topic"))
    api_version = models.CharField(max_length=20, blank=True, verbose_name=_("API version"))
    triggered_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Triggered at"))
    payload = models.JSONField(verbose_name=_("Payload"))
    status = models.CharField(
        max_length=20,
        choices=WebhookEventStatus.choices,
        default=WebhookEventStatus.RECEIVED,
        db_index=True,
        verbose_name=_("Status"),
    )
    attempts = models.PositiveSmallIntegerField(default=0, verbose_name=_("Attempts"))
    error = models.TextField(blank=True, verbose_name=_("Error"))
    received_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Received at"))
    processed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Processed at"))

    class Meta:
        verbose_name = _("Shopify webhook event")
        verbose_name_plural = _("Shopify webhook events")
        ordering = ["-received_at"]

    def __str__(self) -> str:
        return f"{self.topic} ({self.webhook_id})"


class ShopifyLocationMap(models.Model):
    """A store's Shopify location, and the ERP warehouse whose stock it shows."""

    store = models.ForeignKey(
        ShopifyStore, on_delete=models.CASCADE, related_name="locations", verbose_name=_("Shopify store")
    )
    location_id = models.CharField(max_length=255, verbose_name=_("Shopify location ID"))
    name = models.CharField(max_length=255, verbose_name=_("Shopify location"))
    is_active = models.BooleanField(default=True, verbose_name=_("Active in Shopify"))
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="shopify_locations",
        verbose_name=_("Warehouse"),
        help_text=_("ERP warehouse whose stock this location shows. Leave empty to not sync it."),
    )

    class Meta:
        verbose_name = _("Shopify location")
        verbose_name_plural = _("Shopify locations")
        ordering = ["store", "name"]
        constraints = [
            models.UniqueConstraint(fields=["store", "location_id"], name="uniq_shopify_location_per_store"),
            # Two locations fed by one warehouse would offer its stock twice.
            models.UniqueConstraint(
                fields=["store", "warehouse"],
                condition=models.Q(warehouse__isnull=False),
                name="uniq_shopify_warehouse_per_store",
                violation_error_message=_("This warehouse already feeds another location of the store."),
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        if self.warehouse_id and self.store_id and self.warehouse.company_id != self.store.company_id:
            raise ValidationError({"warehouse": _("Choose a warehouse of the store's company.")})


class VariantLinkStatus(models.TextChoices):
    MATCHED = "matched", _("Matched")
    NO_SKU = "no_sku", _("No SKU in Shopify")
    NOT_IN_ERP = "not_in_erp", _("Not in the ERP")
    AMBIGUOUS = "ambiguous", _("SKU used more than once")


class ShopifyVariantLink(models.Model):
    """
    One Shopify product variant and the ERP item with the same SKU.

    Matching uses the variant's SKU against ``InventoryItem.code`` within the
    store's company only. A SKU shared by several variants of the store is
    left unlinked (AMBIGUOUS): linking it twice would sync the same stock twice.
    """

    store = models.ForeignKey(
        ShopifyStore, on_delete=models.CASCADE, related_name="variant_links", verbose_name=_("Shopify store")
    )
    product_id = models.CharField(max_length=255, verbose_name=_("Shopify product ID"))
    variant_id = models.CharField(max_length=255, verbose_name=_("Shopify variant ID"))
    inventory_item_id = models.CharField(max_length=255, blank=True, verbose_name=_("Shopify inventory item ID"))
    sku = models.CharField(max_length=255, blank=True, verbose_name=_("SKU"))
    product_title = models.CharField(max_length=255, blank=True, verbose_name=_("Product"))
    variant_title = models.CharField(max_length=255, blank=True, verbose_name=_("Variant"))
    item = models.ForeignKey(
        "inventory.InventoryItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shopify_variant_links",
        verbose_name=_("ERP item"),
    )
    status = models.CharField(
        max_length=20,
        choices=VariantLinkStatus.choices,
        default=VariantLinkStatus.NOT_IN_ERP,
        db_index=True,
        verbose_name=_("Status"),
    )
    synced_at = models.DateTimeField(verbose_name=_("Synced at"))
    pushed_signature = models.CharField(
        max_length=64,
        blank=True,
        editable=False,
        help_text=_("Fingerprint of the price, cost and SKU last sent to Shopify."),
    )
    archived_from = models.CharField(
        max_length=20,
        blank=True,
        editable=False,
        help_text=_(
            "Status the product had before the ERP archived it because the item stopped being "
            "sold online; restored when it is sold online again."
        ),
    )

    class Meta:
        verbose_name = _("Shopify product link")
        verbose_name_plural = _("Shopify product links")
        ordering = ["store", "product_title", "variant_title"]
        constraints = [
            models.UniqueConstraint(fields=["store", "variant_id"], name="uniq_shopify_variant_per_store"),
            models.UniqueConstraint(
                fields=["store", "item"],
                condition=models.Q(item__isnull=False),
                name="uniq_shopify_item_per_store",
            ),
        ]

    def __str__(self) -> str:
        title = self.product_title
        if self.variant_title and self.variant_title != "Default Title":
            title = f"{title} – {self.variant_title}"
        return f"{title} ({self.sku or '—'})"


class ShopifyCustomerLink(models.Model):
    """The ERP client a store's Shopify customer was matched or created as."""

    store = models.ForeignKey(
        ShopifyStore, on_delete=models.CASCADE, related_name="customer_links", verbose_name=_("Shopify store")
    )
    customer_id = models.CharField(max_length=255, verbose_name=_("Shopify customer ID"))
    client = models.ForeignKey(
        "sales.Client", on_delete=models.CASCADE, related_name="shopify_customer_links", verbose_name=_("Client")
    )

    class Meta:
        verbose_name = _("Shopify customer link")
        verbose_name_plural = _("Shopify customer links")
        constraints = [
            models.UniqueConstraint(fields=["store", "customer_id"], name="uniq_shopify_customer_per_store"),
        ]

    def __str__(self) -> str:
        return f"{self.client} ({self.customer_id})"


class ShopifyOrder(models.Model):
    """A store's Shopify order and the ERP quote it became."""

    store = models.ForeignKey(
        ShopifyStore, on_delete=models.CASCADE, related_name="orders", verbose_name=_("Shopify store")
    )
    order_id = models.CharField(max_length=255, verbose_name=_("Shopify order ID"))
    name = models.CharField(max_length=100, blank=True, verbose_name=_("Order"))
    quote = models.OneToOneField(
        "sales.Quote",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shopify_order",
        verbose_name=_("Quote"),
    )
    placed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Placed at"))
    invoice = models.ForeignKey(
        "invoices.SalesInvoice",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Invoice"),
        help_text=_("The invoice of the whole order, when the store invoices orders once paid."),
    )
    shipping_address = models.ForeignKey(
        "sales.ClientAddress",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Ships to"),
        help_text=_("The client address the order's delivery orders ship to."),
    )
    financial_status = models.CharField(max_length=40, blank=True, verbose_name=_("Payment status"))
    is_cancelled = models.BooleanField(default=False, verbose_name=_("Cancelled"))
    shopify_updated_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Updated in Shopify"))
    currency = models.CharField(max_length=3, blank=True, verbose_name=_("Currency"))
    shopify_total = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, verbose_name=_("Shopify total")
    )
    erp_total = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, verbose_name=_("ERP total")
    )
    totals_match = models.BooleanField(
        default=True,
        db_index=True,
        verbose_name=_("Totals match"),
        help_text=_("Whether the quote's total equals what Shopify charged; differences need review."),
    )
    refunded_total = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, verbose_name=_("Refunded")
    )
    error = models.TextField(blank=True, verbose_name=_("Error"))
    invoicing_error = models.TextField(
        blank=True,
        verbose_name=_("Invoicing error"),
        help_text=_("Why the order couldn't be invoiced automatically; it is retried every few minutes."),
    )
    synced_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Synced at"))

    class Meta:
        verbose_name = _("Shopify order")
        verbose_name_plural = _("Shopify orders")
        ordering = ["-shopify_updated_at"]
        constraints = [
            models.UniqueConstraint(fields=["store", "order_id"], name="uniq_shopify_order_per_store"),
        ]

    def __str__(self) -> str:
        return self.name or self.order_id


class FulfillmentCancellation(models.TextChoices):
    REVIEW = "review", _("Cancelled in Shopify, to review")
    RETURNED = "returned", _("Goods back in the warehouse")
    LOST = "lost", _("Goods lost on the way")


class ShopifyFulfillment(models.Model):
    """A shipment of a store's order, and the ERP delivery order it matches.

    Created from either side: dispatching a delivery order in the ERP pushes
    a fulfillment to Shopify, or a fulfillment made in Shopify creates the
    delivery order. The link stops either side from being created twice.

    A shipment cancelled in Shopify is never undone in the ERP by itself:
    only staff know whether the goods came back or were lost on the way.
    """

    store = models.ForeignKey(
        ShopifyStore, on_delete=models.CASCADE, related_name="fulfillments", verbose_name=_("Shopify store")
    )
    order = models.ForeignKey(
        ShopifyOrder, on_delete=models.CASCADE, related_name="fulfillments", verbose_name=_("Shopify order")
    )
    fulfillment_id = models.CharField(max_length=255, blank=True, verbose_name=_("Shopify fulfillment ID"))
    delivery_order = models.OneToOneField(
        "sales.DeliveryOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shopify_fulfillment",
        verbose_name=_("Delivery order"),
    )
    error = models.TextField(blank=True, verbose_name=_("Error"))
    synced_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Synced at"))
    cancellation = models.CharField(
        max_length=20,
        choices=FulfillmentCancellation.choices,
        blank=True,
        db_index=True,
        verbose_name=_("Cancellation"),
        help_text=_(
            "Set when the shipment is cancelled in Shopify. Staff then record whether the goods "
            "are back in the warehouse or were lost, and the order can be shipped again."
        ),
    )
    cancelled_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Cancelled in Shopify at"))

    class Meta:
        verbose_name = _("Shopify fulfillment")
        verbose_name_plural = _("Shopify fulfillments")
        ordering = ["-synced_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["store", "fulfillment_id"],
                condition=~models.Q(fulfillment_id=""),
                name="uniq_shopify_fulfillment_per_store",
            ),
        ]

    def __str__(self) -> str:
        return self.fulfillment_id or str(self.delivery_order)


class RefundStatus(models.TextChoices):
    NO_CREDIT = "no_credit", _("Nothing to credit")
    PENDING = "pending", _("Waiting for the invoice")
    REVIEW = "review", _("To review")
    CREDITED = "credited", _("Credit note created")


class ShopifyRefund(models.Model):
    """A refund of a store's order, and the credit note it became.

    A refund made before the goods were invoiced only takes them off the
    order's quote. One made after needs a credit note, created or left for
    review as the store chooses (``ShopifyStore.refund_handling``).
    """

    store = models.ForeignKey(
        ShopifyStore, on_delete=models.CASCADE, related_name="refunds", verbose_name=_("Shopify store")
    )
    order = models.ForeignKey(
        ShopifyOrder, on_delete=models.CASCADE, related_name="refunds", verbose_name=_("Shopify order")
    )
    refund_id = models.CharField(max_length=255, verbose_name=_("Shopify refund ID"))
    refunded_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Refunded at"))
    amount = models.DecimalField(max_digits=12, decimal_places=2, verbose_name=_("Amount"))
    # What was refunded, as read from Shopify: {"lines": [{"item", "quantity", "rate"}], "shipping": {"net", "rate"}}
    detail = models.JSONField(default=dict, blank=True, editable=False)
    status = models.CharField(
        max_length=20, choices=RefundStatus.choices, default=RefundStatus.PENDING, db_index=True,
        verbose_name=_("Status"),
    )
    credit_note = models.ForeignKey(
        "invoices.SalesInvoice",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Credit note"),
    )
    note = models.TextField(blank=True, verbose_name=_("Note"))
    processed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Processed at"))

    class Meta:
        verbose_name = _("Shopify refund")
        verbose_name_plural = _("Shopify refunds")
        ordering = ["-refunded_at"]
        constraints = [
            models.UniqueConstraint(fields=["store", "refund_id"], name="uniq_shopify_refund_per_store"),
        ]

    def __str__(self) -> str:
        return f"{self.order} − {self.amount}"
