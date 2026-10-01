import logging
from decimal import Decimal
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext_lazy as _

from sales.models import (
    DeliveryOrder,
    DeliveryOrderItem,
    DeliveryOrderStatus,
    Quote,
    QuoteStatus,
)
from invoices import line_sources
from invoices.models import SalesInvoice, SalesInvoiceItem, SalesInvoiceStatus, AppliedTax
from invoices.services.sales_invoice_payment import resolve_sales_invoice_payment_method

logger = logging.getLogger(__name__)


class DeliveryOrderService:
    """
    Service for creating Delivery Orders from ProjectFile sources and
    for creating SalesInvoices from Delivery Orders.

    Supports two invoicing strategies:
    1. Direct: ProjectFile → SalesInvoice (existing flow, unchanged)
    2. Via Delivery Order: ProjectFile → DeliveryOrder → SalesInvoice

    Items can be sourced from:
    - Configurations (from accepted quotes)
    - Direct Configurations (shop/work-order flow, no quote)
    - QuoteInventoryItems (inventory items attached to quotes)
    """

    @classmethod
    def get_deliverable_items(cls, project_file, exclude_delivery_order=None, include_exhausted=False):
        """
        Returns all items eligible for delivery from a ProjectFile.
        Subtracts already-delivered quantities from existing delivery orders.

        Returns a list of dicts, each with:
        - source_content_type_id, source_object_id: GenericFK identifiers
        - source_type: str label ('configuration', 'direct_configuration', 'inventory_item')
        - description, reference: display fields
        - total_quantity: total available quantity
        - delivered_quantity: quantity already in non-cancelled delivery orders
        - remaining_quantity: total_quantity - delivered_quantity
        - unit_price: price per unit (Decimal)
        - discount_percentage: discount (Decimal)
        - source_object: the actual Django model instance

        A quote line with a negative quantity (a deduction, a return) is
        deliverable too: it is "remaining" while its quantity has not yet
        been put on a delivery order, so the delivery order adds up to the
        quote like the quote does.

        Pricing spreads the *remaining value* of each source over its
        remaining quantity (see ``_price_remaining``), so the delivery order
        that ships the rest of a line always carries the rest of its value —
        whether the earlier orders split it by quantity or by price, and
        whether or not the quote was updated in between.

        Args:
            exclude_delivery_order: leave this order's own lines out of the
                delivered quantities/values (used to re-derive them).
            include_exhausted: also return sources with nothing left to
                deliver (``remaining_quantity`` 0 or over-delivered).
        """
        from configurator.models import Configuration, ConfigurationStatus
        from sales.models import QuoteInventoryItem

        config_ct = ContentType.objects.get_for_model(Configuration)
        inv_item_ct = ContentType.objects.get_for_model(QuoteInventoryItem)

        # Gather all already-delivered quantities by (content_type_id, object_id)
        delivered_map = cls._get_delivered(project_file, exclude_delivery_order)

        items = []

        def add(entry, key, total_qty, unit_price, total_value, discount_pct):
            delivered = delivered_map.get(key, {})
            delivered_qty = delivered.get("quantity", Decimal("0"))
            remaining = total_qty - delivered_qty
            if not include_exhausted and not cls._has_remaining(total_qty, remaining):
                return
            entry.update({
                'total_quantity': total_qty,
                'delivered_quantity': delivered_qty,
                'remaining_quantity': remaining,
                'unit_price': cls._price_remaining(
                    unit_price, total_qty, total_value, discount_pct, delivered,
                ),
                'discount_percentage': discount_pct,
            })
            items.append(entry)

        def config_entry(config, quote_id, source_type, description):
            total_qty = config.invoice_quantity
            # Gross (pre-discount) value — the quote's discount is carried
            # onto the delivery order explicitly as the header discount by
            # create_delivery_order, never baked into item prices.
            config_sub = config.subtotal_price.amount
            add(
                {
                    'source_content_type_id': config_ct.id,
                    'source_object_id': config.id,
                    'source_type': source_type,
                    'description': description,
                    'reference': f"CFG-{config.id}",
                    'source_object': config,
                    'quote_id': quote_id,
                },
                (config_ct.id, config.id),
                total_qty,
                (config_sub / total_qty).quantize(Decimal("0.01")),
                config_sub,
                Decimal("0.00"),
            )

        # 1. Configurations from accepted quotes
        accepted_quotes = Quote.objects.filter(
            project_file=project_file,
            status=QuoteStatus.ACCEPTED,
        )

        for quote in accepted_quotes:
            for config in quote.configurations.all():
                config_entry(config, quote.id, 'configuration', f"{config.name} (Q#{quote.id})")

            # Inventory items attached to the quote
            for inv_item in quote.inventory_items.select_related("inventory_item").all():
                inv_name = (
                    inv_item.inventory_item.name
                    if inv_item.inventory_item_id
                    else f"Inventory Item #{inv_item.pk}"
                )
                add(
                    {
                        'source_content_type_id': inv_item_ct.id,
                        'source_object_id': inv_item.id,
                        'source_type': 'inventory_item',
                        'description': f"{inv_name} (Q#{quote.id})",
                        'reference': inv_item.inventory_item.code if inv_item.inventory_item_id else "",
                        'source_object': inv_item,
                        'quote_id': quote.id,
                    },
                    (inv_item_ct.id, inv_item.id),
                    inv_item.quantity or Decimal("0"),
                    inv_item.unit_price.amount if inv_item.unit_price else Decimal("0"),
                    inv_item.line_total.amount if inv_item.line_total else Decimal("0"),
                    inv_item.discount if inv_item.discount else Decimal("0"),
                )

        # 2. Direct configurations (shop/work-order flow, NOT via a quote)
        direct_configs = Configuration.objects.filter(
            project_file=project_file,
            quote__isnull=True,
            status__in=[
                ConfigurationStatus.SHOP,
                ConfigurationStatus.COMPLETED,
                ConfigurationStatus.CONVERTED,
                ConfigurationStatus.PRODUCTION_DONE,
            ],
        )
        for config in direct_configs:
            config_entry(config, None, 'direct_configuration', config.name)

        return items

    @staticmethod
    def _has_remaining(total_qty, remaining):
        """Whether ``remaining`` still leaves something to deliver of a line
        ordered as ``total_qty``: a positive line is pending while it has not
        been fully (or over-) delivered, a negative one while it has not been
        fully applied. A zero remainder never is."""
        if remaining == 0:
            return False
        return (remaining > 0) == (total_qty > 0)

    @staticmethod
    def _price_remaining(unit_price, total_qty, total_value, discount_pct, delivered):
        """Unit price for what is left of a source line.

        The value not yet delivered — the line's value on the quote minus
        what the existing delivery orders carry for it — is spread over the
        quantity not yet delivered, so the order that completes the line gets
        exactly the rest of its value. Lines with nothing delivered yet (or
        nothing left) keep the plain quote ``unit_price``.

        ``total_value`` is the line's value net of its own line discount
        (``discount_pct``), as delivery order lines are priced gross and
        carry that percentage themselves. Quantities delivered by legacy
        (net-priced) orders have no comparable value, so they are taken at
        the quote price.
        """
        delivered_qty = delivered.get("quantity", Decimal("0"))
        remaining_qty = total_qty - delivered_qty
        factor = (Decimal("100") - discount_pct) / Decimal("100")
        if not total_qty or not delivered_qty or not remaining_qty or not factor:
            return unit_price

        legacy_qty = delivered_qty - delivered.get("priced_quantity", Decimal("0"))
        remaining_value = (
            total_value
            - delivered.get("value", Decimal("0"))
            - (total_value / total_qty * legacy_qty)
        )
        return (remaining_value / remaining_qty / factor).quantize(Decimal("0.01"))

    @classmethod
    def _get_delivered(cls, project_file, exclude_delivery_order=None):
        """
        Returns ``{(content_type_id, object_id): {quantity, value,
        priced_quantity}}`` over the top-level lines of all non-cancelled
        delivery orders linked to this project file.

        ``value`` is the sum of the line totals of self-contained orders and
        ``priced_quantity`` the quantity they cover; legacy net-priced orders
        add to ``quantity`` only.
        """
        delivered = {}
        existing_dos = DeliveryOrder.objects.filter(
            project_file=project_file,
        ).exclude(status=DeliveryOrderStatus.CANCELLED)
        if exclude_delivery_order is not None:
            existing_dos = existing_dos.exclude(pk=exclude_delivery_order.pk)

        for do in existing_dos:
            priced = cls._is_self_contained(do)
            for item in do.items.top_level().filter(source_content_type__isnull=False):
                key = (item.source_content_type_id, item.source_object_id)
                entry = delivered.setdefault(key, {
                    "quantity": Decimal("0"),
                    "value": Decimal("0"),
                    "priced_quantity": Decimal("0"),
                })
                entry["quantity"] += item.quantity
                if priced:
                    entry["priced_quantity"] += item.quantity
                    entry["value"] += item.line_total.amount if item.line_total else Decimal("0")

        return delivered

    @classmethod
    def _get_delivered_quantities(cls, project_file):
        """
        Returns a dict of {(content_type_id, object_id): total_delivered_quantity}
        for all non-cancelled delivery orders linked to this project file.
        """
        return {
            key: entry["quantity"]
            for key, entry in cls._get_delivered(project_file).items()
        }

    @classmethod
    @transaction.atomic
    def create_delivery_order(
        cls,
        project_file,
        company,
        client,
        items_data,
        date=None,
        notes="",
    ):
        """
        Creates a DeliveryOrder with items, validating quantities against
        remaining deliverable amounts.

        Args:
            project_file: The ProjectFile
            company: Company
            client: Client
            items_data: List of dicts with:
                - source_content_type_id: ContentType ID
                - source_object_id: Source object PK
                - description: str
                - quantity: Decimal
                - unit_price: Decimal
                - discount_percentage: Decimal (optional)
                - reference: str (optional)
            date: Date for the delivery order (defaults to today)
            notes: str

        Returns:
            DeliveryOrder instance
        """
        from invoices.services.sales_invoice_service import SalesInvoiceService

        if not items_data:
            raise ValidationError(_("At least one item is required."))

        next_do = SalesInvoiceService._generate_next_delivery_order_data(company)

        delivery_order = DeliveryOrder.objects.create(
            company=company,
            number=next_do['number'],
            incremental_id=next_do['incremental_id'],
            date=date or timezone.now().date(),
            client=client,
            project_file=project_file,
            notes=notes,
            status=DeliveryOrderStatus.DRAFT,
        )

        for item_data in items_data:
            DeliveryOrderItem.objects.create(
                delivery_order=delivery_order,
                description=item_data['description'],
                quantity=item_data.get('quantity', Decimal("1")),
                unit_price=item_data.get('unit_price', Decimal("0")),
                discount_percentage=item_data.get('discount_percentage', Decimal("0")),
                source_content_type_id=item_data.get('source_content_type_id'),
                source_object_id=item_data.get('source_object_id'),
                reference=item_data.get('reference', ''),
            )

        # Carry the source quotes' discounts and taxes onto the delivery order,
        # making it a self-contained document.
        cls.recompute_discounts_and_taxes(delivery_order)
        cls._fill_shipping_defaults(delivery_order)

        logger.info(
            "Created DeliveryOrder %s from ProjectFile %s. Items: %d, "
            "Subtotal: %s, Discount: %s, Tax: %s, Total: %s",
            delivery_order.number,
            project_file.reference,
            len(items_data),
            delivery_order.subtotal_amount,
            delivery_order.discount_amount,
            delivery_order.tax_amount,
            delivery_order.total_amount,
        )

        return delivery_order

    @classmethod
    def _fill_shipping_defaults(cls, delivery_order):
        """Plan the delivery from the source quotes and ship to the client's
        default shipping address. The latest estimated date wins: the order
        can only leave once everything on it is ready."""
        from sales.models import ClientAddress

        quote_ids = {
            cls._source_taxes_and_quote(item)[1]
            for item in delivery_order.items.top_level()
        } - {None}
        dates = [
            d for d in Quote.objects.filter(pk__in=quote_ids)
            .values_list("estimated_delivery_date", flat=True) if d
        ]
        addresses = ClientAddress.objects.filter(client=delivery_order.client).order_by("-is_default", "pk")
        delivery_order.planned_delivery_date = max(dates) if dates else None
        delivery_order.delivery_address = (
            addresses.filter(address_type="shipping").first() or addresses.first()
        )
        delivery_order.save(update_fields=["planned_delivery_date", "delivery_address", "updated_at"])

    @classmethod
    @transaction.atomic
    def sync_from_sources(cls, delivery_order):
        """Brings a delivery order back in line with its sources after the
        quotes changed, without deleting and re-creating it.

        Each quote-sourced top-level line is re-priced (description,
        reference, unit price and line discount) exactly as the wizard would
        price it today, given what the *other* delivery orders of the
        expediente carry — so an order that ships the rest of a line gets the
        rest of its value. Quantities are kept: they are what was shipped.
        Lines whose source no longer exists (or whose quote is no longer
        accepted) are removed, and sources with something left to deliver
        that are on no other order are added with their remaining quantity.
        Manual lines are left alone. The header discount and taxes are then
        re-derived.

        Returns ``{"updated": n, "added": n, "removed": n}``.
        """
        from configurator.models import Configuration
        from sales.models import QuoteInventoryItem

        # A cancelled invoice bills nothing: the order is free again
        if cls.has_active_invoice(delivery_order):
            raise ValidationError(
                _("This delivery order is already invoiced. Unlink or delete the invoice before syncing it.")
            )
        if delivery_order.project_file_id is None:
            raise ValidationError(_("This delivery order is not linked to a project file."))
        if delivery_order.status == DeliveryOrderStatus.CANCELLED:
            raise ValidationError(_("A cancelled delivery order cannot be synced."))

        sources = {
            (i['source_content_type_id'], i['source_object_id']): i
            for i in cls.get_deliverable_items(
                delivery_order.project_file,
                exclude_delivery_order=delivery_order,
                include_exhausted=True,
            )
        }
        syncable_cts = {
            ContentType.objects.get_for_model(Configuration).id,
            ContentType.objects.get_for_model(QuoteInventoryItem).id,
        }

        counts = {"updated": 0, "added": 0, "removed": 0}
        for line in delivery_order.items.top_level().filter(source_content_type_id__in=syncable_cts):
            data = sources.pop((line.source_content_type_id, line.source_object_id), None)
            if data is None:
                line.delete()
                counts["removed"] += 1
                continue
            fields = {
                "description": data["description"],
                "reference": data["reference"],
                "unit_price": data["unit_price"],
                "discount_percentage": data["discount_percentage"],
            }
            if any(
                (getattr(line, name).amount if name == "unit_price" else getattr(line, name)) != value
                for name, value in fields.items()
            ):
                for name, value in fields.items():
                    setattr(line, name, value)
                line.save()
                counts["updated"] += 1

        for data in sources.values():
            if not cls._has_remaining(data["total_quantity"], data["remaining_quantity"]):
                continue
            DeliveryOrderItem.objects.create(
                delivery_order=delivery_order,
                description=data["description"],
                quantity=data["remaining_quantity"],
                unit_price=data["unit_price"],
                discount_percentage=data["discount_percentage"],
                source_content_type_id=data["source_content_type_id"],
                source_object_id=data["source_object_id"],
                reference=data["reference"],
            )
            counts["added"] += 1

        cls.recompute_discounts_and_taxes(delivery_order)

        logger.info(
            "Synced DeliveryOrder %s from its sources: %d updated, %d added, %d removed. "
            "Subtotal: %s, Discount: %s, Tax: %s, Total: %s",
            delivery_order.number,
            counts["updated"], counts["added"], counts["removed"],
            delivery_order.subtotal_amount,
            delivery_order.discount_amount,
            delivery_order.tax_amount,
            delivery_order.total_amount,
        )
        return counts

    @classmethod
    @transaction.atomic
    def recompute_discounts_and_taxes(cls, delivery_order):
        """Re-derives the header discount, the AppliedTax rows and the totals
        from the delivery order's lines. Called on creation and whenever the
        lines change.

        Only for self-contained delivery orders (gross-priced lines) — see
        ``_is_self_contained``.
        """
        from invoices.services.discounts import QuoteDiscountResolver, SourceLine, source_kind

        line_taxes = []    # (line_total, source_taxes, quote_id, kind)
        source_lines = []
        quote_taxes_cache: dict[int, list] = {}

        # Component lines carry no value; their parent does.
        for item in delivery_order.items.top_level():
            line_total = item.line_total.amount if item.line_total else Decimal("0.00")
            source_taxes, quote_id = cls._source_taxes_and_quote(item, quote_taxes_cache)
            if quote_id is None and not source_taxes:
                # A line added by hand (portes, extras) is taxed as the quote
                # would tax a line of its own.
                source_taxes = cls._manual_line_taxes(delivery_order, item)
            source_id = cls._build_source_id(item)
            kind = source_kind(source_id)
            source_lines.append(SourceLine(source_id, line_total, quote_id, kind))
            line_taxes.append((line_total, source_taxes, quote_id, kind))

        quote_ids = {line.quote_id for line in source_lines if line.quote_id is not None}
        quotes = list(Quote.objects.filter(id__in=quote_ids))
        resolution = QuoteDiscountResolver().resolve(quotes, source_lines)

        delivery_order.discount_amount = resolution.discount_amount
        delivery_order.discount_breakdown = resolution.breakdown
        delivery_order.save(update_fields=[
            'discount_amount', 'discount_amount_currency', 'discount_breakdown',
        ])

        # Aggregate the sources' taxes onto the delivery order, computed on
        # each line's discounted base.
        tax_agg: dict[tuple, dict] = {}
        for line_total, source_taxes, quote_id, kind in line_taxes:
            frac = resolution.taxable_fraction(quote_id, kind)
            taxable = (line_total * (Decimal("1") - frac)).quantize(Decimal("0.01"))
            for tax in source_taxes:
                if tax.selected_percentage <= 0:
                    continue
                key = (tax.tax_type_id, tax.tax_type.behavior, tax.selected_percentage)
                rate = tax.selected_percentage / Decimal("100")
                calc = (taxable * rate).quantize(Decimal("0.01"))

                if key not in tax_agg:
                    tax_agg[key] = {
                        "tax_type": tax.tax_type,
                        "selected_percentage": tax.selected_percentage,
                        "base_amount": Decimal("0.00"),
                        "calculated_amount": Decimal("0.00"),
                    }
                tax_agg[key]["base_amount"] += taxable
                tax_agg[key]["calculated_amount"] += calc

        do_ct = ContentType.objects.get_for_model(delivery_order)
        delivery_order.applied_taxes.all().delete()
        for order, entry in enumerate(tax_agg.values()):
            AppliedTax.objects.create(
                content_type=do_ct,
                object_id=delivery_order.id,
                tax_type=entry["tax_type"],
                selected_percentage=entry["selected_percentage"],
                base_amount=entry["base_amount"],
                calculated_amount=entry["calculated_amount"],
                order=order,
            )

        delivery_order.recalculate_totals()
        return delivery_order

    @classmethod
    def _source_taxes_and_quote(cls, delivery_order_item, quote_taxes_cache=None):
        """The AppliedTax records and owning quote id of a line's source
        object, resolved from its GenericFK. ``([], None)`` for lines with no
        quote-side source (manual lines, direct configurations).

        The taxes mirror how the source quote taxed the line, so the
        delivery order (and the invoice copied from it) agrees with the
        quote:

        * Client-tax mode (``company.auto_apply_client_taxes``): the quote
          applies its header taxes to the whole discounted subtotal, whatever
          each source carries — a configuration without AppliedTax rows of
          its own is still taxed there. The quote's header rows are used.
        * Source mode: each source's own AppliedTax rows, as the quote's
          ``SourceTaxStrategy`` does.

        ``quote_taxes_cache`` memoises the per-quote header lookup across the
        lines of one recompute.
        """
        from configurator.models import Configuration
        from sales.models import QuoteInventoryItem

        ct_id = delivery_order_item.source_content_type_id
        obj_id = delivery_order_item.source_object_id
        if not ct_id or not obj_id:
            return [], None

        config_ct = ContentType.objects.get_for_model(Configuration)
        inv_item_ct = ContentType.objects.get_for_model(QuoteInventoryItem)

        if ct_id == config_ct.id:
            source_ct, source_model = config_ct, Configuration
        elif ct_id == inv_item_ct.id:
            source_ct, source_model = inv_item_ct, QuoteInventoryItem
        else:
            return [], None

        quote_id = (
            source_model.objects.filter(pk=obj_id)
            .values_list("quote_id", flat=True).first()
        )

        if quote_id is not None:
            quote_taxes = cls._quote_header_taxes(quote_id, quote_taxes_cache)
            if quote_taxes is not None:
                return quote_taxes, quote_id

        source_taxes = list(
            AppliedTax.objects.filter(
                content_type=source_ct, object_id=obj_id,
            ).select_related("tax_type")
        )
        return source_taxes, quote_id

    @classmethod
    def _manual_line_taxes(cls, delivery_order, item):
        """Taxes for a line with no quote-side source, mirroring how a quote
        line gets its taxes (``QuoteInventoryItem._copy_taxes_from_item``):
        the client's taxes in client-tax mode, else the inventory item's own,
        else the company's defaults."""
        from sales.models import Client

        company = delivery_order.company
        if company is not None and company.auto_apply_client_taxes and delivery_order.client_id:
            client_ct = ContentType.objects.get_for_model(Client)
            client_taxes = list(
                AppliedTax.objects.filter(
                    content_type=client_ct, object_id=delivery_order.client_id,
                ).select_related("tax_type").order_by("order", "id")
            )
            if client_taxes:
                return client_taxes

        if item.inventory_item_id:
            item_taxes = list(item.inventory_item.get_taxes())
            if item_taxes:
                return item_taxes

        if company is None:
            return []
        return [
            AppliedTax(tax_type=cdt.tax_type, selected_percentage=cdt.selected_percentage)
            for cdt in company.company_default_taxes.select_related("tax_type").all()
        ]

    @classmethod
    def _quote_header_taxes(cls, quote_id, cache=None):
        """The quote's own AppliedTax rows when its company taxes quotes in
        client-tax mode; ``None`` when the per-source rows should be used
        instead (source mode, or a quote whose header was never synced)."""
        if cache is not None and quote_id in cache:
            return cache[quote_id]

        result = None
        quote = Quote.objects.filter(pk=quote_id).select_related("company").first()
        if quote is not None and quote.company_id and quote.company.auto_apply_client_taxes:
            quote_ct = ContentType.objects.get_for_model(Quote)
            header_taxes = list(
                AppliedTax.objects.filter(
                    content_type=quote_ct, object_id=quote_id,
                ).select_related("tax_type").order_by("order", "id")
            )
            if header_taxes:
                result = header_taxes

        if cache is not None:
            cache[quote_id] = result
        return result

    @classmethod
    def get_invoiceable_delivery_orders(cls, project_file):
        """
        Returns delivery orders linked to a ProjectFile that are not billed
        yet: no invoice, or only a cancelled one (see has_active_invoice).
        """
        return DeliveryOrder.objects.filter(
            Q(sales_invoice__isnull=True)
            | Q(sales_invoice__status=SalesInvoiceStatus.CANCELLED),
            project_file=project_file,
        ).exclude(
            status=DeliveryOrderStatus.CANCELLED,
        ).order_by('date', 'number')

    @staticmethod
    def has_active_invoice(delivery_order):
        """Whether the delivery order is billed: linked to an invoice that is
        not cancelled. A cancelled invoice bills nothing, so its delivery
        order can be invoiced again; the new invoice takes over the link."""
        return bool(
            delivery_order.sales_invoice_id
            and delivery_order.sales_invoice.status != SalesInvoiceStatus.CANCELLED
        )

    @classmethod
    @transaction.atomic
    def create_invoice_from_delivery_orders(
        cls,
        delivery_orders,
        company,
        client,
        date_issued,
        due_date=None,
        notes="",
        payment_method=None,
    ):
        """
        Creates a SalesInvoice from one or more DeliveryOrders.

        Each top-level delivery order item becomes an invoice line item
        (component lines are skipped) and the delivery order's header
        discount is copied as the invoice's.
        Taxes are copied from the source objects' AppliedTax records.
        The expediente's advances are then deducted on the invoice (the
        default plan); the delivery orders themselves are left as they are.

        Args:
            delivery_orders: queryset or list of DeliveryOrder instances
            company: Company
            client: Client
            date_issued: date
            due_date: date (optional)
            notes: str
            payment_method: str (optional)

        Returns:
            SalesInvoice instance
        """
        from configurator.models import Configuration
        from invoices.services.advances import AdvanceDeductionService, AdvanceInvoiceService
        from invoices.services.project_file_invoice_service import ProjectFileInvoiceService
        from sales.models import QuoteInventoryItem

        # Expedientes first, then their advances, before any numbering lock
        project_file_ids = [do.project_file_id for do in delivery_orders]
        ProjectFileInvoiceService.lock_project_files(project_file_ids)
        AdvanceInvoiceService.lock_advances(project_file_ids)
        AdvanceInvoiceService.check_no_proforma_advance(project_file_ids)

        for do in delivery_orders:
            if cls.has_active_invoice(do):
                raise ValidationError(
                    _("Delivery order %(number)s is already invoiced (%(invoice)s).") % {
                        "number": do.number,
                        "invoice": do.sales_invoice.display_number,
                    }
                )

        resolved_payment_method = resolve_sales_invoice_payment_method(
            client=client,
            explicit=payment_method,
        )

        project_file = None
        for do in delivery_orders:
            if do.project_file_id:
                project_file = do.project_file
                break

        invoice = SalesInvoice.objects.create(
            company=company,
            status=SalesInvoiceStatus.PROFORMA,
            invoice_number=None,
            date_issued=date_issued,
            due_date=due_date,
            client=client,
            project_file=project_file,
            notes=notes,
            payment_method=resolved_payment_method,
        )

        config_ct = ContentType.objects.get_for_model(Configuration)
        inv_item_ct = ContentType.objects.get_for_model(QuoteInventoryItem)

        subtotal = Decimal("0.00")
        invoice_discount = Decimal("0.00")
        invoice_discount_breakdown = []
        tax_agg = {}  # (tax_type_id, behavior, percentage) → aggregated amounts

        for do in delivery_orders:
            # Self-contained DOs are copied as-is; legacy net-priced DOs
            # resolve taxes from the source objects.
            self_contained = cls._is_self_contained(do)

            # Component lines are not billable: the parent line carries the
            # price, so they stay on the delivery order only.
            for item in do.items.top_level():
                source_id = cls._build_source_id(item)

                inv_item = SalesInvoiceItem.objects.create(
                    invoice=invoice,
                    description=item.description,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    discount_percentage=item.discount_percentage,
                    source_id=source_id,
                    reference=item.reference,
                )
                line_total = inv_item.line_total.amount
                subtotal += line_total

                if self_contained:
                    continue  # taxes come from the DO's own AppliedTax rows

                # Legacy path: resolve taxes from the source object
                source_taxes = []
                if item.source_content_type_id == config_ct.id and item.source_object_id:
                    source_taxes = list(
                        AppliedTax.objects.filter(
                            content_type=config_ct,
                            object_id=item.source_object_id,
                        ).select_related("tax_type")
                    )
                elif item.source_content_type_id == inv_item_ct.id and item.source_object_id:
                    source_taxes = list(
                        AppliedTax.objects.filter(
                            content_type=inv_item_ct,
                            object_id=item.source_object_id,
                        ).select_related("tax_type")
                    )

                for tax in source_taxes:
                    if tax.selected_percentage <= 0:
                        continue
                    key = (tax.tax_type_id, tax.tax_type.behavior, tax.selected_percentage)
                    rate = tax.selected_percentage / Decimal("100")
                    calc = (line_total * rate).quantize(Decimal("0.01"))

                    if key not in tax_agg:
                        tax_agg[key] = {
                            "tax_type": tax.tax_type,
                            "selected_percentage": tax.selected_percentage,
                            "base_amount": Decimal("0.00"),
                            "calculated_amount": Decimal("0.00"),
                        }
                    tax_agg[key]["base_amount"] += line_total
                    tax_agg[key]["calculated_amount"] += calc

            if self_contained:
                if do.discount_amount and do.discount_amount.amount > 0:
                    invoice_discount += do.discount_amount.amount
                    invoice_discount_breakdown.extend(do.discount_breakdown or [])
                for tax in do.applied_taxes.select_related("tax_type").order_by("order", "id"):
                    if tax.selected_percentage <= 0:
                        continue
                    key = (tax.tax_type_id, tax.tax_type.behavior, tax.selected_percentage)
                    if key not in tax_agg:
                        tax_agg[key] = {
                            "tax_type": tax.tax_type,
                            "selected_percentage": tax.selected_percentage,
                            "base_amount": Decimal("0.00"),
                            "calculated_amount": Decimal("0.00"),
                        }
                    tax_agg[key]["base_amount"] += tax.base_amount.amount
                    tax_agg[key]["calculated_amount"] += tax.calculated_amount.amount

            # Link the delivery order to the invoice
            do.sales_invoice = invoice
            do.save(update_fields=["sales_invoice"])

        # Create aggregated AppliedTax records on the invoice
        ct = ContentType.objects.get_for_model(invoice)
        computed_total_tax = Decimal("0.00")

        for order, entry in enumerate(tax_agg.values()):
            # Retentions (IRPF) reduce the total, as in SalesInvoice.recalculate_totals
            if entry["tax_type"].behavior == "add":
                computed_total_tax += entry["calculated_amount"]
            else:
                computed_total_tax -= entry["calculated_amount"]
            AppliedTax.objects.create(
                content_type=ct,
                object_id=invoice.id,
                tax_type=entry["tax_type"],
                selected_percentage=entry["selected_percentage"],
                base_amount=entry["base_amount"],
                calculated_amount=entry["calculated_amount"],
                order=order,
            )

        invoice.subtotal_amount = subtotal
        invoice.discount_amount = invoice_discount
        invoice.discount_breakdown = invoice_discount_breakdown
        invoice.tax_amount = computed_total_tax
        invoice.total_amount = subtotal - invoice_discount + computed_total_tax
        invoice.save(update_fields=[
            'subtotal_amount', 'discount_amount', 'discount_amount_currency',
            'discount_breakdown', 'tax_amount', 'total_amount',
        ])

        # The invoice's total goes down by the advances; the delivery order's does not
        AdvanceDeductionService.apply(invoice)

        logger.info(
            "Created SalesInvoice #%s from %d DeliveryOrders. "
            "Amount: %s (subtotal: %s, tax: %s)",
            invoice.id,
            len(list(delivery_orders)),
            invoice.total_amount,
            invoice.subtotal_amount,
            invoice.tax_amount,
        )

        return invoice

    @classmethod
    def _is_self_contained(cls, delivery_order):
        """Whether this DO carries its own discount/tax data (gross lines +
        header discount + AppliedTax rows), as opposed to a legacy DO whose
        config lines were priced net of the quote discount."""
        return bool(
            delivery_order.applied_taxes.exists()
            or (delivery_order.discount_amount and delivery_order.discount_amount.amount > 0)
        )

    @classmethod
    def _build_source_id(cls, delivery_order_item):
        """
        Builds a source_id string for a SalesInvoiceItem from a DeliveryOrderItem's
        GenericFK, matching the format used by the existing ProjectFileInvoiceService.
        """
        from configurator.models import Configuration
        from sales.models import QuoteInventoryItem

        if not delivery_order_item.source_content_type_id:
            return ""

        config_ct = ContentType.objects.get_for_model(Configuration)
        inv_item_ct = ContentType.objects.get_for_model(QuoteInventoryItem)

        if delivery_order_item.source_content_type_id == config_ct.id:
            return line_sources.source_id(line_sources.CONFIG, delivery_order_item.source_object_id)
        elif delivery_order_item.source_content_type_id == inv_item_ct.id:
            return line_sources.source_id(line_sources.INV_ITEM, delivery_order_item.source_object_id)

        return f"source_{delivery_order_item.source_content_type_id}_{delivery_order_item.source_object_id}"

    @classmethod
    def get_delivery_summary(cls, project_file):
        """
        Returns a dict with the delivery status for a ProjectFile:
        - deliverable_items: list of items eligible for delivery
        - delivery_orders: list of existing delivery orders
        - total_deliverable_value: sum of all deliverable item values
        - total_delivered_value: sum of all delivered item values
        """
        items = cls.get_deliverable_items(project_file)

        existing_dos = DeliveryOrder.objects.filter(
            project_file=project_file,
        ).exclude(
            status=DeliveryOrderStatus.CANCELLED,
        ).order_by('-date')

        total_deliverable = sum(
            (i['unit_price'] * i['total_quantity'] * (1 - i['discount_percentage'] / 100) for i in items),
            Decimal("0.00"),
        )
        # Quote-sourced lines are legacy financing lines, not goods.
        quote_ct = ContentType.objects.get_for_model(Quote)
        total_delivered = Decimal("0.00")
        for do in existing_dos:
            for item in do.items.all():
                if item.source_content_type_id == quote_ct.id:
                    continue
                if item.line_total:
                    total_delivered += item.line_total.amount

        return {
            'deliverable_items': items,
            'delivery_orders': existing_dos,
            'total_deliverable_value': total_deliverable,
            'total_delivered_value': total_delivered,
        }
