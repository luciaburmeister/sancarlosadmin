from decimal import Decimal

from django.db import models
from django.db.models.functions import Coalesce, Greatest
from django.utils.translation import gettext_lazy as _
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django_countries.fields import CountryField
from djmoney.models.fields import MoneyField
from timestamps.models import Timestampable
from django.db import transaction
from django.core.exceptions import ValidationError

from company.models import CompanyOwnedModel
from materials.models import Provider
from .choices import LocationType, ItemType, MovementType
from configurator.utils.codegen import next_scoped_code


class InventoryStatus(models.TextChoices):
    ACTIVE = "ACTIVE", _("Active")
    REQUESTED = "REQUESTED", _("Requested")
    RESERVED = "RESERVED", _("Reserved")


class InventoryCategory(Timestampable, CompanyOwnedModel):
    name = models.CharField(_("Name"), max_length=120)

    class Meta:
        verbose_name = _("Inventory Category")
        verbose_name_plural = _("Inventory Categories")
        constraints = [
            models.UniqueConstraint(
                fields=["company", "name"],
                name="uniq_inventory_category_company_name",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "name"], name="idx_invcat_company_name"),
        ]

    def __str__(self):
        return self.name


class Warehouse(Timestampable, CompanyOwnedModel):
    code = models.CharField(_("Code"), max_length=30, db_index=True)
    name = models.CharField(_("Name"), max_length=120)
    country = CountryField(_("Country"), blank=True)
    city = models.CharField(_("City"), max_length=120, blank=True)
    postal_code = models.CharField(_("Postal Code"), max_length=30, blank=True)
    address_line1 = models.CharField(_("Address Line 1"), max_length=200, blank=True)
    address_line2 = models.CharField(_("Address Line 2"), max_length=200, blank=True)
    is_active = models.BooleanField(_("Active"), default=True)

    class Meta:
        verbose_name = _("Warehouse")
        verbose_name_plural = _("Warehouses")
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"],
                name="uniq_warehouse_company_code",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "code"], name="idx_wh_company_code"),
        ]

    def __str__(self):
        return f"{self.code} | {self.name}"


class StockLocation(Timestampable):
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        verbose_name=_("Warehouse"),
        on_delete=models.PROTECT,
        related_name="locations",
        null=True,
        blank=True,
    )
    location_type = models.CharField(
        _("Location Type"),
        max_length=20,
        choices=LocationType.choices,
        default=LocationType.BIN,
        db_index=True,
    )
    content_type = models.ForeignKey(
        ContentType,
        verbose_name=_("Content Type"),
        on_delete=models.PROTECT,
        related_name="inventory_stock_locations",
        null=True,
        blank=True,
    )
    object_id = models.PositiveBigIntegerField(_("Object ID"), null=True, blank=True)
    source_object = GenericForeignKey("content_type", "object_id")

    name = models.CharField(_("Name"), max_length=120)
    code = models.CharField(_("Code"), max_length=40, blank=True)
    is_active = models.BooleanField(_("Active"), default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["warehouse", "code"],
                name="uniq_stock_location_warehouse_code",
            )
        ]
        verbose_name = _("Stock Location")
        verbose_name_plural = _("Stock Locations")

    def __str__(self):
        return f"{self.source_object} ({self.code or self.name})" if self.source_object else (self.code or self.name)

    def clean(self):
        super().clean()
        if bool(self.content_type_id) ^ bool(self.object_id):
            raise ValidationError(
                {"content_type": _("Select both Content Type and Object ID, or leave both empty.")}
            )


class InventoryItemQuerySet(models.QuerySet):
    """Queryset for :model:`inventory.InventoryItem`.

    Also the single home of the stock-position rules. ``incoming_expression``
    and ``reserved_expression`` define what those figures mean; everything
    else routes through them — ``with_computed_stock_position`` annotates
    them, the cache refresh in ``inventory.services.stock_position`` writes
    their result into ``incoming_quantity`` / ``reserved_quantity``, and the
    drift check compares the stored value back against them. One definition
    is what makes the cached columns trustworthy: there is no second
    implementation to fall out of step with.
    """

    #: Output field for the quantity aggregates. PO lines, quote lines and
    #: delivery lines are all DecimalField(10, 3); the sums need a wider
    #: decimal to hold them without overflowing.
    QUANTITY_FIELD = models.DecimalField(max_digits=16, decimal_places=3)
    ZERO = models.Value(Decimal("0"), output_field=QUANTITY_FIELD)

    # ── Expressions ──────────────────────────────────────────────────────

    @staticmethod
    def stock_on_hand_expression():
        """Net of every stock movement recorded for the item.

        Not cached: it is a single indexed aggregate over an append-only
        table, and caching it would mean writing the item row on every
        movement.
        """
        return Coalesce(
            models.Subquery(
                ItemTransaction.objects
                .filter(item=models.OuterRef("pk"))
                .values("item")
                .annotate(total=models.Sum("quantity_change"))
                .values("total")[:1],
                output_field=models.IntegerField(),
            ),
            models.Value(0),
        )

    @classmethod
    def stock_value_expression(cls):
        """Stock on hand priced at cost: ``stock_on_hand_expression() × cost_price``."""
        return cls.stock_on_hand_expression() * models.F("cost_price")

    @classmethod
    def incoming_expression(cls):
        """Ordered from a supplier but not yet received.

        Only APPROVED orders count: drafts and orders awaiting approval have
        not been bought, and a CLOSED order has nothing left on the way.
        """
        from purchasing.models import PurchaseOrderItem, PurchaseOrderStatus

        return Coalesce(
            models.Subquery(
                PurchaseOrderItem.objects
                .filter(
                    inventory_item=models.OuterRef("pk"),
                    purchase_order__status=PurchaseOrderStatus.APPROVED,
                    quantity__gt=models.F("received_quantity"),
                )
                .values("inventory_item")
                .annotate(
                    total=models.Sum(
                        models.F("quantity") - models.F("received_quantity"),
                        output_field=cls.QUANTITY_FIELD,
                    )
                )
                .values("total")[:1],
                output_field=cls.QUANTITY_FIELD,
            ),
            cls.ZERO,
        )

    #: How many levels of bill-of-materials nesting the reserved figure
    #: follows: a kit's components, their components, and so on. Each level
    #: is one more correlated subquery, so the depth is fixed rather than
    #: recursive; kits nested deeper than this reserve nothing below it.
    RESERVED_BOM_DEPTH = 3

    @classmethod
    def reserved_expression(cls):
        """Promised on accepted quotes for open projects, not yet delivered.

        Stock physically leaves the warehouse when a delivery order is
        dispatched, so what is still reserved is the accepted-quote quantity
        minus whatever those quote lines have already shipped
        (``DeliveryOrderItem.objects.shipped()``). A pending or prepared
        delivery order keeps its goods reserved; a cancelled one has given
        them back.

        A quote has no state after ACCEPTED, and many flows invoice quote
        lines directly without ever raising a delivery order, so acceptance
        alone would reserve stock forever. The project file's lifecycle is
        the release valve: lines on COMPLETED or CANCELLED projects no longer
        count. Quotes without a project file stay reserved until delivered or
        rejected.

        A quoted kit promises its components too: selling it ships them
        (``DeliveryOrderItem._deduct_inventory`` cascades through the bill of
        materials), so each component is reserved for ``BOM quantity × the
        parent's reservation`` on top of whatever it is quoted for directly.
        The parent's figure already nets out deliveries, which is why the
        component lines a delivery order materialises are not counted again
        here. Nesting is followed ``RESERVED_BOM_DEPTH`` levels down.
        """
        return cls._reserved_expression("pk", cls.RESERVED_BOM_DEPTH)

    @classmethod
    def pending_to_serve_expression(cls):
        """Accepted-quote quantity not yet delivered, without the kit cascade.

        The same net ``reserved_expression`` starts from — quoted on an open
        project, minus what those lines have already shipped — and nothing
        else. A component promised only because a kit that contains it was
        sold does not count here.
        """
        return cls._own_reserved_expression("pk")

    @classmethod
    def _own_reserved_expression(cls, item_ref_name):
        """Direct reservation for the item referenced by ``item_ref_name``."""
        from management.models import ProjectFileStatus
        from sales.models import (
            DeliveryOrderItem,
            QuoteInventoryItem,
            QuoteStatus,
        )

        def item_ref(depth):
            """OuterRef back at the item, wrapped once per subquery level
            between the enclosing queryset and where it is used."""
            ref = models.OuterRef(item_ref_name)
            for _ in range(depth - 1):
                ref = models.OuterRef(ref)
            return ref

        def accepted_quote_lines(ref):
            """Quote lines for this item on accepted quotes for open projects."""
            return QuoteInventoryItem.objects.filter(
                inventory_item=ref,
                quote__status=QuoteStatus.ACCEPTED,
            ).exclude(
                quote__project_file__status__in=[
                    ProjectFileStatus.COMPLETED,
                    ProjectFileStatus.CANCELLED,
                ],
            )

        quoted = models.Subquery(
            accepted_quote_lines(item_ref(1))
            .values("inventory_item")
            .annotate(total=models.Sum("quantity", output_field=cls.QUANTITY_FIELD))
            .values("total")[:1],
            output_field=cls.QUANTITY_FIELD,
        )

        delivered = models.Subquery(
            DeliveryOrderItem.objects
            .filter(
                source_content_type=ContentType.objects.get_for_model(QuoteInventoryItem),
                source_object_id__in=accepted_quote_lines(item_ref(2)).values("pk"),
            )
            .shipped()
            .values("source_content_type")  # constant key: one row per subquery
            .annotate(total=models.Sum("quantity", output_field=cls.QUANTITY_FIELD))
            .values("total")[:1],
            output_field=cls.QUANTITY_FIELD,
        )

        # Clamped at zero: a hand-built delivery line can ship more than was
        # quoted, and a negative reservation would be meaningless.
        return Greatest(
            Coalesce(quoted, cls.ZERO) - Coalesce(delivered, cls.ZERO),
            cls.ZERO,
            output_field=cls.QUANTITY_FIELD,
        )

    @classmethod
    def _reserved_expression(cls, item_ref_name, bom_depth):
        """The reserved figure for the item referenced by ``item_ref_name``
        on the enclosing queryset — ``"pk"`` when annotating InventoryItem
        itself, ``"parent_id"`` when the expression is nested inside a query
        over BillOfMaterialsLine to value a parent's reservation.
        """
        own = cls._own_reserved_expression(item_ref_name)
        if bom_depth <= 0:
            return own

        def item_ref(depth):
            """OuterRef back at the item, wrapped once per subquery level
            between the enclosing queryset and where it is used."""
            ref = models.OuterRef(item_ref_name)
            for _ in range(depth - 1):
                ref = models.OuterRef(ref)
            return ref

        # Reserved through the kits this item is a component of: each BOM
        # line contributes its quantity × the parent's own reservation, the
        # parent's figure being this same expression one level up.
        as_component = models.Subquery(
            BillOfMaterialsLine.objects
            .filter(component=item_ref(1))
            .annotate(parent_reserved=cls._reserved_expression("parent_id", bom_depth - 1))
            .values("component")
            .annotate(
                total=models.Sum(
                    models.ExpressionWrapper(
                        models.F("quantity") * models.F("parent_reserved"),
                        output_field=cls.QUANTITY_FIELD,
                    ),
                    output_field=cls.QUANTITY_FIELD,
                )
            )
            .values("total")[:1],
            output_field=cls.QUANTITY_FIELD,
        )
        return models.ExpressionWrapper(
            own + Coalesce(as_component, cls.ZERO),
            output_field=cls.QUANTITY_FIELD,
        )

    # ── Annotations ──────────────────────────────────────────────────────

    def with_stock_on_hand(self):
        """Annotate ``stock_on_hand_total`` so a list of items costs one query.

        Without it the ``stock_on_hand`` property runs its own aggregate per
        row.
        """
        return self.annotate(stock_on_hand_total=self.stock_on_hand_expression())

    def with_computed_stock_position(self):
        """Annotate the live, uncached figures as ``computed_incoming`` and
        ``computed_reserved``.

        This is the expensive path — two correlated subqueries per row. It
        exists to audit the cached columns (see ``find_drift``); day-to-day
        reads should use the stored ``incoming_quantity`` /
        ``reserved_quantity`` fields instead.
        """
        return self.annotate(
            computed_incoming=self.incoming_expression(),
            computed_reserved=self.reserved_expression(),
        )




def _item_prefix(item_type: str) -> str:
    return {
        ItemType.STOCKABLE: "STK",
        ItemType.SERVICE: "SRV",
        ItemType.FINISHED_PRODUCT: "FP",
        ItemType.OWN_PRODUCTION: "OP",
    }.get(item_type, "ITM")


class InventoryItem(Timestampable, CompanyOwnedModel):
    code = models.CharField(_("Code"), max_length=20, blank=True, db_index=True)
    name = models.CharField(_("Name"), max_length=200, db_index=True, null=True, blank=True)
    description = models.TextField(_("Description"), blank=True)
    item_type = models.CharField(
        _("Item Type"),
        max_length=30,
        choices=ItemType.choices,
        default=ItemType.STOCKABLE,
        db_index=True,
    )
    default_warehouse = models.ForeignKey(
        "inventory.Warehouse",
        verbose_name=_("Warehouse"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="items",
    )
    business_unit = models.ForeignKey(
        "accounting.BusinessUnit",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_items",
        verbose_name=_("Business Unit"),
        help_text=_("Decides whether its sales are goods or services; invoice lines take it when created."),
    )
    preferred_supplier = models.ForeignKey(
        Provider,
        verbose_name=_("Preferred Supplier"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="supplied_items",
    )
    invoice = models.ForeignKey(
        "invoices.Invoice",
        verbose_name=_("Invoice"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inventory_items",
    )
    product_content_type = models.ForeignKey(
        ContentType,
        verbose_name=_("Product Content Type"),
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inventory_items",
    )
    product_object_id = models.PositiveBigIntegerField(_("Product Object ID"), null=True, blank=True)
    product_object = GenericForeignKey("product_content_type", "product_object_id")

    image = models.ImageField(_("Image"), upload_to="inventory/items/images/", null=True, blank=True)

    selling_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Selling Price"),
        null=True,
        blank=True,
    )

    cost_price = MoneyField(
        max_digits=12,
        decimal_places=2,
        default=0,
        default_currency="EUR",
        verbose_name=_("Cost Price"),
        null=True,
        blank=True,
        help_text=_("Purchase/cost price used for margin calculations."),
    )

    min_stock = models.PositiveIntegerField(
        _("Minimum Stock"),
        null=True,
        blank=True,
        help_text=_("Alert threshold: an order reminder will be shown when stock falls below this level."),
    )

    # ── Cached stock position ───────────────────────────────────────────────
    # Maintained by inventory.services.stock_position, which recomputes them
    # from source (never by applying deltas) whenever a purchase order, quote,
    # delivery order or project file moves. Written only by that service:
    # save() leaves them out of full saves (see CACHED_STOCK_FIELDS) so an
    # instance loaded before a refresh cannot write a stale value back over it.
    # A missed event therefore leaves a stale row that the next refresh — or
    # `manage.py refresh_stock_position` — repairs, rather than permanent drift.

    # ``has_components`` is maintained the same way — by inventory.signals,
    # from the bill of materials itself — so it is listed here too and a full
    # save cannot write a stale value back over it.

    CACHED_STOCK_FIELDS = (
        "incoming_quantity",
        "reserved_quantity",
        "stock_position_refreshed_at",
        "has_components",
    )

    exclude_from_commission = models.BooleanField(
        _("No commission"),
        default=False,
        help_text=_(
            "Lines of this item earn agents no commission (e.g. portes): they "
            "are left out of the commission base of quotes and invoices."
        ),
    )

    has_components = models.BooleanField(
        _("Composite Item"),
        default=False,
        editable=False,
        db_index=True,
        help_text=_(
            "Whether this item is assembled from others. Set from the bill of "
            "materials, never by hand."
        ),
    )

    incoming_quantity = models.DecimalField(
        _("Incoming Quantity"),
        max_digits=16,
        decimal_places=3,
        default=Decimal("0"),
        editable=False,
        help_text=_("Ordered on approved purchase orders but not yet received."),
    )
    reserved_quantity = models.DecimalField(
        _("Reserved Quantity"),
        max_digits=16,
        decimal_places=3,
        default=Decimal("0"),
        editable=False,
        help_text=_("Promised on accepted quotes but not yet delivered."),
    )
    stock_position_refreshed_at = models.DateTimeField(
        _("Stock Position Refreshed At"),
        null=True,
        blank=True,
        editable=False,
        help_text=_("When the incoming and reserved figures were last recomputed."),
    )

    objects = InventoryItemQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company", "code"],
                name="uniq_item_company_code",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "item_type"], name="idx_item_company_type"),
        ]
        verbose_name = _("Item")
        verbose_name_plural = _("Items")
        permissions = [
            ("can_reset_stock", _("Can reset item stock to zero")),
        ]

    def __str__(self):
        return f"{self.code} | {self.name}"

    def clean(self):
        super().clean()
        if self.business_unit_id and self.business_unit.company_id != self.company_id:
            raise ValidationError({"business_unit": _("The business unit must belong to the same company.")})
        if self.item_type == ItemType.SERVICE and self.default_warehouse_id:
            raise ValidationError({"default_warehouse": _("Services cannot have a warehouse.")})
        if self.item_type in (ItemType.STOCKABLE, ItemType.FINISHED_PRODUCT, ItemType.OWN_PRODUCTION) and not self.default_warehouse_id:
            raise ValidationError({"default_warehouse": _("This item type requires a warehouse.")})

        # OWN_PRODUCTION items must be linked to a ConfigurationPreset
        if self.item_type == ItemType.OWN_PRODUCTION:
            from configurator.models import ConfigurationPreset
            preset_ct = ContentType.objects.get_for_model(ConfigurationPreset)
            if self.product_content_type_id and self.product_content_type_id != preset_ct.pk:
                raise ValidationError({
                    "product_content_type": _("Own Production items can only be linked to a Configuration Preset.")
                })

    @transaction.atomic
    def save(self, *args, **kwargs):
        if self.item_type == ItemType.SERVICE:
            self.default_warehouse = None

        if not self.code:
            prefix = _item_prefix(self.item_type)
            self.code = next_scoped_code(
                model=InventoryItem,
                scope_filter={"company": self.company, "item_type": self.item_type},
                prefix=prefix,
                width=5,
                separator="-",
            )

        # editable=False only hides the cached columns from forms; a full save
        # would still write the instance's copy of them back. Narrow the save
        # to everything else so an admin edit or a spreadsheet import that
        # loaded this row before a refresh cannot clobber the refreshed value.
        if (
            not self._state.adding
            and kwargs.get("update_fields") is None
            and not kwargs.get("force_insert")
        ):
            deferred = self.get_deferred_fields()
            kwargs["update_fields"] = [
                field.name
                for field in self._meta.concrete_fields
                if not field.primary_key
                and field.name not in self.CACHED_STOCK_FIELDS
                and field.attname not in deferred
            ]

        super().save(*args, **kwargs)

    @property
    def stock_on_hand(self) -> int:
        """Net stock from every recorded movement.

        Prefers the ``stock_on_hand_total`` annotation from
        ``with_stock_on_hand()`` when the instance carries one, so a list costs
        one query. Annotations are not refreshed by ``refresh_from_db()``, so
        an instance that records movements itself should not be loaded that
        way if it then needs to read the result.
        """
        annotated = getattr(self, "stock_on_hand_total", None)
        if annotated is not None:
            return int(annotated)
        return self.transactions.aggregate(total=models.Sum("quantity_change")).get("total") or 0

    @property
    def available_quantity(self) -> Decimal:
        """Stock on hand that is not already promised to an accepted quote."""
        return Decimal(self.stock_on_hand) - self.reserved_quantity

    @property
    def stock_value(self):
        """Stock on hand priced at cost. See ``stock_on_hand`` for caching."""
        return self.cost_price * self.stock_on_hand

    @property
    def is_low_stock(self) -> bool:
        """True when min_stock is set and current stock is at or below it.

        A composite never qualifies: it holds no stock of its own, so its
        figure would sit at zero and trip any threshold forever. What can
        really run out are its components, each watched on its own row.
        """
        if self.is_composite:
            return False
        return self.min_stock is not None and self.stock_on_hand <= self.min_stock

    def get_taxes(self):
        """Return AppliedTax records attached to this item via GenericFK."""
        from invoices.models import AppliedTax
        ct = ContentType.objects.get_for_model(self)
        return AppliedTax.objects.filter(
            content_type=ct, object_id=self.pk
        ).select_related("tax_type").order_by("order")

    @property
    def is_composite(self) -> bool:
        """True when this item is assembled from other items.

        A composite item is a recipe rather than goods on a shelf: what a
        warehouse actually holds is its components, so consuming one has to
        move those and never the item itself.

        Reads the stored flag rather than the lines, so the question costs
        nothing wherever it is asked — once per component of every BOM walk,
        and once per row of the item list.
        """
        return self.has_components

    def deduct_bom_components(
        self,
        quantity: int,
        warehouse,
        *,
        reference_object=None,
        user=None,
        note: str = "",
        _seen: set | None = None,
    ):
        """Deduct stock for the goods this item is built from.

        Walks the :model:`inventory.BillOfMaterialsLine` rows attached to this
        item, recursing so nested BOMs are handled automatically. Only items
        that are not themselves composite get a stock-out ``ItemTransaction``:
        those are the ones a warehouse actually holds.

        Args:
            quantity: Number of *parent* units being consumed.
            warehouse: Warehouse to deduct from (falls back to the
                component's ``default_warehouse`` when available).
            reference_object: Optional originating document (Quote, etc.).
            user: User performing the operation.
            note: Free-text note added to each transaction.
            _seen: Internal set used to prevent infinite loops from
                circular BOM references.  Callers should **not** pass this.
        """
        from .choices import MovementType

        self._move_bom_components(
            quantity,
            warehouse,
            movement_type=MovementType.OUT,
            sign=-1,
            reference_object=reference_object,
            user=user,
            note=note,
            _seen=_seen,
        )

    def restock_bom_components(
        self,
        quantity: int,
        warehouse,
        *,
        reference_object=None,
        user=None,
        note: str = "",
        _seen: set | None = None,
    ):
        """Put back every BOM component consumed for ``quantity`` parent units.

        Mirror image of :meth:`deduct_bom_components`, used when the document
        that consumed the components is cancelled or deleted.  It records
        compensating stock-in transactions instead of deleting the original
        ones, so the movement history stays auditable.  Arguments are the
        same as for the deduction.
        """
        from .choices import MovementType

        self._move_bom_components(
            quantity,
            warehouse,
            movement_type=MovementType.IN,
            sign=1,
            reference_object=reference_object,
            user=user,
            note=note,
            _seen=_seen,
        )

    def transfer_bom_components(
        self,
        quantity: int,
        source_warehouse,
        destination_warehouse,
        *,
        user=None,
        note: str = "",
    ):
        """Move the goods behind ``quantity`` units of this item from one
        warehouse to another.

        Both warehouses were picked by hand, so the components move exactly
        between them: a component's own default warehouse gets no say here,
        or a transfer could quietly take from and put back into the same
        place and move nothing at all.
        """
        for warehouse, sign in ((source_warehouse, -1), (destination_warehouse, 1)):
            self._move_bom_components(
                quantity,
                warehouse,
                movement_type=MovementType.TRANSFER,
                sign=sign,
                user=user,
                note=note,
                pin_warehouse=True,
            )

    def _move_bom_components(
        self,
        quantity: int,
        warehouse,
        *,
        movement_type: str,
        sign: int,
        reference_object=None,
        user=None,
        note: str = "",
        pin_warehouse: bool = False,
        _seen: set | None = None,
    ):
        """Walk this item's BOM tree, moving each non-composite component's
        stock by ``sign × line quantity``.  Shared body of the deduct, restock
        and transfer helpers.

        ``pin_warehouse`` holds every movement in ``warehouse`` instead of
        letting a component's own default warehouse win.
        """
        if _seen is None:
            _seen = set()
        if self.pk in _seen:
            return  # circular reference guard
        _seen.add(self.pk)

        if movement_type == MovementType.TRANSFER:
            default_note = f"BOM transfer for {self.code} ×{quantity}"
        elif sign < 0:
            default_note = f"BOM deduction for {self.code} ×{quantity}"
        else:
            default_note = f"BOM restock for {self.code} ×{quantity}"

        bom_lines = self.bom_lines.select_related(
            "component", "component__default_warehouse"
        )
        for line in bom_lines:
            component = line.component
            total_qty = line.quantity * quantity
            target_wh = warehouse if pin_warehouse else (component.default_warehouse or warehouse)

            # A composite component is a recipe in its own right: the goods
            # are the ones further down its bill of materials, so moving it
            # here as well would move the same goods twice.
            if not component.is_composite:
                ref_ct = ref_id = None
                if reference_object is not None:
                    ref_ct = ContentType.objects.get_for_model(reference_object)
                    ref_id = reference_object.pk

                ItemTransaction.objects.create(
                    item=component,
                    warehouse=target_wh,
                    movement_type=movement_type,
                    quantity_change=sign * total_qty,
                    reference_content_type=ref_ct,
                    reference_object_id=ref_id,
                    user=user,
                    note=note or default_note,
                )

            # Recurse for nested BOMs
            component._move_bom_components(
                total_qty,
                target_wh,
                movement_type=movement_type,
                sign=sign,
                reference_object=reference_object,
                user=user,
                note=note,
                pin_warehouse=pin_warehouse,
                _seen=_seen,
            )



class BillOfMaterialsLine(Timestampable):
    """Links a parent InventoryItem to a component InventoryItem with a quantity.

    Example: 1 unit of 'Window Assembly' requires 2 units of 'Glass Panel'
    and 4 units of 'Aluminium Profile'.
    """

    parent = models.ForeignKey(
        "inventory.InventoryItem",
        verbose_name=_("Parent Item"),
        on_delete=models.CASCADE,
        related_name="bom_lines",
        help_text=_("The composite / assembled item."),
    )
    component = models.ForeignKey(
        "inventory.InventoryItem",
        verbose_name=_("Component Item"),
        on_delete=models.PROTECT,
        related_name="used_in_bom",
        help_text=_("The part / sub-item consumed when the parent is built or sold."),
    )
    quantity = models.PositiveIntegerField(
        _("Quantity"),
        default=1,
        help_text=_("How many units of the component are needed per 1 unit of the parent."),
    )

    class Meta:
        verbose_name = _("Bill of Materials Line")
        verbose_name_plural = _("Bill of Materials Lines")
        # The order the lines were added in. Without it the database is free
        # to return them in any order, which would shuffle the components
        # under their parent on a delivery note between one render and the
        # next.
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["parent", "component"],
                name="uniq_bom_parent_component",
            ),
        ]
        indexes = [
            models.Index(fields=["parent"], name="idx_bom_parent"),
        ]

    def __str__(self):
        return f"{self.parent.code} → {self.component.code} ×{self.quantity}"

    def clean(self):
        super().clean()
        if self.parent_id and self.component_id and self.parent_id == self.component_id:
            raise ValidationError(
                {"component": _("A component cannot be the same as the parent item.")}
            )


class ItemProviderInfo(Timestampable):
    """
    Stores a provider-specific reference / SKU for an inventory item.
    Each provider can assign its own unique identifier to the item.
    """

    item = models.ForeignKey(
        "inventory.InventoryItem",
        verbose_name=_("Item"),
        on_delete=models.CASCADE,
        related_name="provider_info",
    )
    provider = models.ForeignKey(
        Provider,
        verbose_name=_("Provider"),
        on_delete=models.CASCADE,
        related_name="item_info",
    )
    provider_ref = models.CharField(
        _("Provider Reference"),
        max_length=100,
        help_text=_("The provider's own code / SKU for this item."),
    )

    class Meta:
        verbose_name = _("Item Provider Info")
        verbose_name_plural = _("Item Provider Info")
        constraints = [
            models.UniqueConstraint(
                fields=["item", "provider_ref"],
                name="uniq_item_provider_ref",
            ),
            models.UniqueConstraint(
                fields=["provider", "provider_ref"],
                name="uniq_provider_item_ref",
            ),
        ]
        indexes = [
            models.Index(fields=["provider_ref"], name="idx_item_provider_ref"),
        ]

    def __str__(self):
        return f"{self.provider.name} → {self.provider_ref}"


class ItemTransaction(Timestampable):
    """
    Tracks every state change / physical movement an inventory item goes through.

    quantity_change: positive = stock in, negative = stock out.
    Use `reference` to link back to the originating document
    (purchase order, production ticket, transfer order, etc.) via a GenericForeignKey.
    """

    item = models.ForeignKey(
        "inventory.InventoryItem",
        verbose_name=_("Item"),
        on_delete=models.CASCADE,
        related_name="transactions",
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        verbose_name=_("Warehouse"),
        on_delete=models.PROTECT,
        related_name="transactions",
    )
    location = models.ForeignKey(
        "inventory.StockLocation",
        verbose_name=_("Stock Location"),
        on_delete=models.SET_NULL,
        related_name="transactions",
        null=True,
        blank=True,
        help_text=_("Specific bin / rack / shelf inside the warehouse (optional)."),
    )
    movement_type = models.CharField(
        _("Movement Type"),
        max_length=10,
        choices=MovementType.choices,
        db_index=True,
    )
    quantity_change = models.IntegerField(
        _("Quantity Change"),
        help_text=_("Positive for stock-in, negative for stock-out."),
    )
    # ── Generic FK so any document can be a source ──────────────────────────
    reference_content_type = models.ForeignKey(
        ContentType,
        verbose_name=_("Reference Type"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="item_transactions",
        help_text=_("Type of the originating document (e.g. PurchaseOrder, ProductionTicket)."),
    )
    reference_object_id = models.PositiveBigIntegerField(
        _("Reference ID"),
        null=True,
        blank=True,
        help_text=_("PK of the originating document."),
    )
    reference_object = GenericForeignKey("reference_content_type", "reference_object_id")

    user = models.ForeignKey(
        "auth.User",
        verbose_name=_("User"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text=_("User who recorded the transaction."),
    )
    note = models.TextField(_("Note"), blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["created_at"], name="idx_itemtx_created_at"),
            models.Index(fields=["movement_type"], name="idx_itemtx_type"),
            models.Index(fields=["item"], name="idx_itemtx_item"),
            models.Index(fields=["warehouse"], name="idx_itemtx_wh"),
            models.Index(
                fields=["reference_content_type", "reference_object_id"],
                name="idx_itemtx_ref",
            ),
        ]
        verbose_name = _("Item Transaction")
        verbose_name_plural = _("Item Transactions")

    def __str__(self):
        return (
            f"{self.created_at:%Y-%m-%d %H:%M} — {self.item} — "
            f"{self.get_movement_type_display()} ({self.quantity_change:+d})"
        )

    def clean(self):
        super().clean()
        if bool(self.reference_content_type_id) ^ bool(self.reference_object_id):
            raise ValidationError(
                {
                    "reference_content_type": _(
                        "Provide both Reference Type and Reference ID, or leave both empty."
                    )
                }
            )


# ── Item Identity Codes (serial numbers, lot IDs, etc.) ─────────────────────

class ItemIdentityCode(Timestampable):
    """
    Tracks an individual identity code (serial number, lot ID, etc.)
    for a specific InventoryItem.  Each code is unique per item.

    Codes can be registered manually from the InventoryItem admin
    or during goods-receipt via PurchaseOrderReceiptItem.  When items
    are shipped out, the code is linked to a DeliveryOrderItem.
    """

    item = models.ForeignKey(
        "inventory.InventoryItem",
        verbose_name=_("Item"),
        on_delete=models.CASCADE,
        related_name="identity_codes",
    )
    code = models.CharField(
        _("Identity Code"),
        max_length=200,
        help_text=_(
            "Unique identifier for this individual unit "
            "(serial number, lot ID, IMEI, etc.)."
        ),
    )

    # ── Receiving traceability ──────────────────────────────────────────
    receipt_item = models.ForeignKey(
        "purchasing.PurchaseOrderReceiptItem",
        verbose_name=_("Receipt Item"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="identity_codes",
        help_text=_(
            "The receipt line through which this unit was received."
        ),
    )
    received_at = models.DateTimeField(
        _("Received At"),
        null=True,
        blank=True,
        help_text=_(
            "Timestamp when this code was registered upon receiving."
        ),
    )

    # ── Delivery traceability ───────────────────────────────────────────
    delivery_order_item = models.ForeignKey(
        "sales.DeliveryOrderItem",
        verbose_name=_("Delivery Order Item"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="identity_codes",
        help_text=_(
            "The delivery-order line this unit was shipped in."
        ),
    )
    delivered_at = models.DateTimeField(
        _("Delivered At"),
        null=True,
        blank=True,
        help_text=_(
            "Timestamp when this code was marked as delivered."
        ),
    )

    note = models.TextField(_("Note"), blank=True)

    class Meta:
        verbose_name = _("Item Identity Code")
        verbose_name_plural = _("Item Identity Codes")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["item", "code"],
                name="uniq_identity_code_per_item",
            ),
        ]
        indexes = [
            models.Index(fields=["code"], name="idx_identity_code"),
            models.Index(fields=["item", "code"], name="idx_identity_item_code"),
        ]

    def __str__(self):
        return f"{self.item.code} — {self.code}"

    @property
    def is_received(self) -> bool:
        """Whether this unit is physically in. Keyed on received_at rather than
        receipt_item so that codes registered without a purchase order behind
        them (historic imports, stock counted in by hand) still count as
        received. save() guarantees received_at whenever receipt_item is set,
        so receipt-linked codes are unaffected."""
        return self.received_at is not None

    @property
    def is_delivered(self) -> bool:
        return self.delivery_order_item_id is not None

    def save(self, *args, **kwargs):
        from django.utils import timezone as tz

        # Auto-set received_at when receipt_item is assigned for the first time
        if self.receipt_item_id and not self.received_at:
            self.received_at = tz.now()

        # Auto-set delivered_at when delivery_order_item is assigned
        if self.delivery_order_item_id and not self.delivered_at:
            self.delivered_at = tz.now()

        # Clear delivered_at if delivery_order_item is removed
        if not self.delivery_order_item_id and self.delivered_at:
            self.delivered_at = None

        super().save(*args, **kwargs)


# ── Keep the original InventoryMovement for backwards compatibility ──────────

class InventoryMovement(Timestampable):
    """Legacy stock movement model — superseded by ItemTransaction.
    Kept to avoid breaking existing migrations / data."""

    movement_type = models.CharField(
        _("Movement Type"),
        max_length=10,
        choices=MovementType.choices,
        db_index=True,
    )
    quantity_change = models.IntegerField(
        _("Quantity Change"),
        help_text=_("Positive for stock-in, negative for stock-out."),
    )
    user = models.ForeignKey(
        "auth.User",
        verbose_name=_("User"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text=_("User who performed the movement."),
    )
    item = models.ForeignKey(
        "inventory.InventoryItem",
        verbose_name=_("Item"),
        on_delete=models.PROTECT,
        related_name="movements",
    )
    warehouse = models.ForeignKey(
        "inventory.Warehouse",
        verbose_name=_("Warehouse"),
        on_delete=models.PROTECT,
        related_name="movements",
    )
    note = models.TextField(_("Note"), blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["created_at"], name="idx_invmov_created_at"),
            models.Index(fields=["movement_type"], name="idx_invmov_type"),
            models.Index(fields=["item"], name="idx_invmov_item"),
            models.Index(fields=["warehouse"], name="idx_invmov_wh"),
        ]
        verbose_name = _("Inventory Movement (legacy)")
        verbose_name_plural = _("Inventory Movements (legacy)")

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} — {self.item} — {self.quantity_change:+d}"