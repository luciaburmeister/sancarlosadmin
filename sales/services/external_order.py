"""
Turning an order from an external sales channel (an online store, a
marketplace) into a client and a quote.

Channel-neutral: an integration such as ``apps.shopify`` translates its own
order format into ``ExternalOrder`` and calls this service, so every channel
produces the same kind of quote, reservation, invoice and credit note.

From there the ERP's normal flow takes over: a paid order's quote is accepted
into an expediente of its own, as the quote acceptance wizard does, and its
delivery orders and invoices are made from that expediente.

The channel already charged the customer, so its amounts are kept exactly:
each line's net total is pinned with ``override_line_total``, its tax comes
from the channel's rate, and the quote is flagged ``prices_from_channel`` so
no discount policy changes it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from django.utils.translation import gettext as _
from djmoney.money import Money

from sales.models import Client, ClientAddress, DeliveryOrder, DeliveryOrderStatus, Quote, QuoteInventoryItem, QuoteStatus

if TYPE_CHECKING:
    from company.models import Company
    from configurator.models import ConfigurationPreset
    from inventory.models import InventoryItem
    from invoices.models import SalesInvoice, TaxType
    from invoices.services.sales_invoice_service import CreditNoteLine
    from management.models import ProjectFile
    from payments.models import Payment, PaymentMethod


class ExternalOrderError(Exception):
    """The order can't be applied automatically; the message says why, for staff."""


class InvoiceKind(StrEnum):
    """Spanish invoice types (TipoFactura)."""

    FULL = "F1"
    SIMPLIFIED = "F2"


@dataclass
class ExternalOrderLine:
    item: InventoryItem  # of the order's company
    quantity: Decimal
    net_total: Decimal  # line total without tax, after the channel's discounts
    tax_rate: Decimal  # percent, e.g. Decimal("21")


@dataclass
class QuoteRequestLine:
    item: InventoryItem  # of the quote's company
    quantity: Decimal
    # Values for the open parameters of the item's configurator preset: a
    # made-to-measure request. None for the item as it is.
    parameters: dict[str, str] | None = None
    # The customer's measurements or requirements for this product.
    notes: str = ""

    @property
    def label(self) -> str:
        """The product as staff recognise it in the quote: its name, and the values chosen for it."""
        name = self.item.name or self.item.code
        return f"{name} ({', '.join(self.parameters.values())})" if self.parameters else name


@dataclass
class ExternalCustomer:
    name: str
    email: str = ""
    phone: str = ""
    tax_identifier: str = ""
    street: str = ""
    city: str = ""
    postal_code: str = ""
    province: str = ""


@dataclass
class ExternalAddress:
    """Where an order ships, and who receives it."""

    street: str
    city: str
    postal_code: str = ""
    province: str = ""
    country: str = ""  # ISO 3166 code, e.g. "ES"
    recipient: str = ""
    company: str = ""
    phone: str = ""


@dataclass
class ExternalOrder:
    company: Company
    client: Client
    reference: str  # the channel's order id, stored as Quote.external_id
    name: str
    date: date
    paid: bool
    tax_type: TaxType  # the channel's rates are recorded under it
    currency: str = "EUR"
    lines: list[ExternalOrderLine] = field(default_factory=list)
    shipping_address: ExternalAddress | None = None


class ExternalOrderService:
    @classmethod
    def find_or_create_client(cls, company: Company, customer: ExternalCustomer) -> Client:
        """The company's client with the same tax id or email, else a new one."""
        clients = Client.objects.filter(company=company)
        client = None
        if customer.tax_identifier:
            client = clients.filter(tax_identifier__iexact=customer.tax_identifier.strip()).first()
        if client is None and customer.email:
            client = clients.filter(email__iexact=customer.email.strip()).order_by("pk").first()
        if client is not None:
            cls.complete_client(client, customer)
            return client

        client = Client.objects.create(
            company=company,
            name=customer.name[:200],
            email=customer.email,
            phone=customer.phone[:50],
            tax_identifier=customer.tax_identifier[:25],
        )
        cls._add_customer_address(client, customer)
        return client

    @classmethod
    def complete_client(cls, client: Client, customer: ExternalCustomer) -> None:
        """Fill in what an existing client is missing from the customer's latest details.

        Only empty fields are filled: a client is maintained by staff, and a
        web order matched by email may belong to a business client whose
        details are set on purpose.
        """
        changed = []
        for name, max_length in (("phone", 50), ("tax_identifier", 25)):
            value = getattr(customer, name).strip()
            if value and not getattr(client, name):
                setattr(client, name, value[:max_length])
                changed.append(name)
        if changed:
            client.save(update_fields=changed)
        if not client.addresses.exists():
            cls._add_customer_address(client, customer)

    @staticmethod
    def _add_customer_address(client: Client, customer: ExternalCustomer) -> None:
        if customer.street and customer.city:
            ClientAddress.objects.create(
                company=client.company,
                client=client,
                street=customer.street[:255],
                city=customer.city[:100],
                postal_code=customer.postal_code[:20],
                province=customer.province[:100],
                is_default=True,
            )

    @staticmethod
    def delivery_address(order: ExternalOrder) -> ClientAddress | None:
        """The client's address the order ships to, added to the client if it is new.

        ``None`` when the order ships nowhere (e.g. collected in store).
        """
        address = order.shipping_address
        if address is None or not (address.street.strip() and address.city.strip()):
            return None
        values = {
            "street": address.street.strip()[:255],
            "city": address.city.strip()[:100],
            "postal_code": address.postal_code.strip()[:20],
            "province": address.province.strip()[:100],
            "country": (address.country or "ES").upper(),
            "contact_name": address.recipient.strip()[:255],
        }
        existing = ClientAddress.objects.filter(
            client=order.client, **{f"{key}__iexact": value for key, value in values.items()}
        ).order_by("-is_default", "pk").first()
        if existing is not None:
            return existing
        return ClientAddress.objects.create(
            company=order.company,
            client=order.client,
            address_type="shipping",
            name=address.company.strip()[:255],
            contact_phone=address.phone.strip()[:50],
            is_default=not order.client.addresses.exists(),
            **values,
        )

    @classmethod
    def ship_pending_deliveries_to(cls, quote: Quote, address: ClientAddress) -> None:
        """Point the quote's delivery orders not dispatched yet at ``address``."""
        pending = cls.delivery_orders(quote).filter(
            status__in=[DeliveryOrderStatus.DRAFT, DeliveryOrderStatus.PREPARED]
        ).exclude(delivery_address=address)
        for delivery_order in pending:
            delivery_order.delivery_address = address
            delivery_order.save(update_fields=["delivery_address", "updated_at"])

    @classmethod
    @transaction.atomic
    def request_quote(
        cls, company: Company, client: Client, lines: Iterable[QuoteRequestLine], name: str, notes: str = ""
    ) -> Quote:
        """A draft quote for what a customer asked to be quoted, priced as a quote made by hand.

        An item line takes the item's price and taxes as usual. A made-to-measure
        line becomes a configuration of the item's preset with the customer's
        values, priced by the configurator as in the preset wizard. Staff adjust
        either before sending. The customer's requirements, for the whole
        request and per product, become the quote's observations.
        """
        from configurator.services.preset_service import create_configuration_from_preset

        lines = list(lines)
        quote = Quote.objects.create(
            company=company,
            client=client,
            project_name=name[:200],
            status=QuoteStatus.DRAFT,
            observations=cls.request_observations(notes, lines) or None,
        )
        for position, line in enumerate(lines):
            if line.parameters is None:
                QuoteInventoryItem.objects.create(
                    quote=quote, order=position, inventory_item=line.item, quantity=line.quantity
                )
                continue
            preset = cls.made_to_measure_preset(line.item)
            if preset is None:
                raise ExternalOrderError(_("%(item)s can't be made to measure.") % {"item": line.item.name})
            try:
                values = preset.with_values(line.parameters)
            except ValidationError as exc:
                labels = dict(preset.product.parameters.values_list("name", "label"))
                raise ExternalOrderError(" ".join(
                    f"{labels.get(name, name)}: {message}"
                    for name, messages in exc.message_dict.items()
                    for message in messages
                )) from exc
            create_configuration_from_preset(
                preset, quote=quote, merged_values=values, client=client, quantity=int(line.quantity)
            )
        return quote

    @staticmethod
    def request_observations(notes: str, lines: Iterable[QuoteRequestLine]) -> str:
        """The request's own notes, then each product's, one per line after its label."""
        parts = [notes.strip()] if notes and notes.strip() else []
        product_notes = [f"• {line.label}: {line.notes.strip()}" for line in lines if line.notes.strip()]
        if product_notes:
            parts.append("\n".join(product_notes))
        return "\n\n".join(parts)

    @staticmethod
    def made_to_measure_preset(item: InventoryItem) -> ConfigurationPreset | None:
        """The item's configurator preset when it leaves parameters for the customer to choose."""
        preset = item.configuration_preset
        return preset if preset is not None and preset.get_missing_parameters() else None

    @classmethod
    @transaction.atomic
    def sync_quote(
        cls, order: ExternalOrder, quote: Quote | None = None, replace_pending_deliveries: bool = False
    ) -> Quote:
        """Create the order's quote, or bring an existing one up to date.

        Unpaid orders stay SENT (no reservation); paid ones are ACCEPTED.
        Lines are rewritten only while none of them is on an active delivery
        order: after that the quote is left for staff to adjust. With
        ``replace_pending_deliveries``, delivery orders still pending shipment
        are cancelled first, so the order can change until it is prepared.
        """
        if quote is None:
            quote = Quote(company=order.company, external_id=order.reference, prices_from_channel=True)
        elif quote.status == QuoteStatus.REJECTED:
            raise ExternalOrderError(_("The order's quote was rejected; it is not reopened automatically."))

        quote.client = order.client
        quote.project_name = order.name[:200]
        quote.date = order.date
        target_status = QuoteStatus.ACCEPTED if order.paid else QuoteStatus.SENT
        if quote.status != QuoteStatus.ACCEPTED:  # an accepted order is never un-accepted here
            quote.status = target_status
        if quote.status == QuoteStatus.ACCEPTED and quote.project_file_id is None:
            from management.services.quote_acceptance import QuoteAcceptanceService

            quote.project_file = QuoteAcceptanceService.open_project_file(quote, order.name)
        quote.save()

        if cls._lines_changed(quote, order):
            if replace_pending_deliveries:
                cls._cancel_pending_deliveries(quote)
            if cls._has_delivery_orders(quote):
                raise ExternalOrderError(
                    _("The order changed after it was put on a delivery order. Update quote %(quote)s by hand.")
                    % {"quote": quote}
                )
            cls._replace_lines(quote, order)
        return quote

    @classmethod
    @transaction.atomic
    def cancel(cls, quote: Quote, cancel_pending_deliveries: bool = False) -> None:
        """Reject the quote, which releases its reservations.

        With ``cancel_pending_deliveries``, delivery orders still pending
        shipment are cancelled with it; any further along stop the cancel.
        """
        if cancel_pending_deliveries:
            cls._cancel_pending_deliveries(quote)
        if cls._has_delivery_orders(quote):
            raise ExternalOrderError(
                _("The order was cancelled after it was put on a delivery order. Review quote %(quote)s by hand.")
                % {"quote": quote}
            )
        if quote.status != QuoteStatus.REJECTED:
            quote.status = QuoteStatus.REJECTED
            quote.save()
        cls._close_project_file(quote)

    @staticmethod
    def _close_project_file(quote: Quote) -> None:
        """Cancel the expediente of a cancelled order when nothing else was done on it."""
        from invoices.models import SalesInvoice, SalesInvoiceStatus
        from management.models import ProjectFileStatus

        project_file = quote.project_file
        if project_file is None or project_file.status == ProjectFileStatus.CANCELLED:
            return
        still_used = (
            project_file.quotes.exclude(status=QuoteStatus.REJECTED).exists()
            or DeliveryOrder.objects.filter(project_file=project_file).exclude(status=DeliveryOrderStatus.CANCELLED).exists()
            or SalesInvoice.objects.filter(project_file=project_file).exclude(status=SalesInvoiceStatus.CANCELLED).exists()
        )
        if not still_used:
            project_file.status = ProjectFileStatus.CANCELLED
            project_file.save(update_fields=["status", "updated_at"])

    @classmethod
    def _lines_changed(cls, quote: Quote, order: ExternalOrder) -> bool:
        current = [
            (line.inventory_item_id, line.quantity, line.override_line_total.amount if line.override_line_total else None)
            for line in quote.inventory_items.all()
        ]
        wanted = [(line.item.pk, line.quantity, line.net_total) for line in order.lines]
        return sorted(current) != sorted(wanted)

    @staticmethod
    def delivery_orders(quote: Quote) -> QuerySet[DeliveryOrder]:
        """The order's delivery orders that are not cancelled: those of its expediente."""
        if quote.project_file_id is None:
            return DeliveryOrder.objects.none()
        return DeliveryOrder.objects.filter(project_file_id=quote.project_file_id).exclude(
            status=DeliveryOrderStatus.CANCELLED
        )

    @classmethod
    def _has_delivery_orders(cls, quote: Quote) -> bool:
        return cls.delivery_orders(quote).exists()

    @classmethod
    def is_locked(cls, quote: Quote) -> bool:
        """Whether the quote's lines are settled: on a delivery order past pending."""
        return cls.delivery_orders(quote).exclude(status=DeliveryOrderStatus.DRAFT).exists()

    # ── Invoicing ────────────────────────────────────────────────────────

    @staticmethod
    def invoice_kind(company: Company, client: Client) -> InvoiceKind:
        """Full (F1) or simplified (F2) invoice for a channel sale.

        Always full for now, as e-invoicing submits every sales invoice. The
        simplified-invoice rules (threshold, series) will be company settings.
        """
        return InvoiceKind.FULL

    @classmethod
    @transaction.atomic
    def invoice(cls, quote: Quote, issue: bool = False, send_email: bool = False, notes: str = "") -> SalesInvoice:
        """Invoice what the order's expediente has left, as its invoice wizard does.

        The order's delivery orders are billed by this invoice, so they are
        not invoiced a second time.
        """
        from invoices.services.advances import AdvanceDeductionService
        from invoices.services.project_file_invoice_service import ProjectFileInvoiceService

        project_file = quote.project_file
        if project_file is None:
            raise ExternalOrderError(_("The order has no expediente to invoice."))
        cls._check_invoice_kind(quote.company, quote.client)
        summary = ProjectFileInvoiceService.get_invoiceable_summary(project_file)
        items = ProjectFileInvoiceService.build_items_for_remaining(summary)
        if not items:
            raise ExternalOrderError(_("Expediente %(reference)s has nothing left to invoice.") % {"reference": project_file.reference})
        invoice = ProjectFileInvoiceService.create_invoice_from_project_file(
            project_file=project_file,
            company=project_file.company,
            client=project_file.client,
            date_issued=timezone.localdate(),
            due_date=None,
            items_data=items,
            notes=notes,
        )
        AdvanceDeductionService.apply(invoice)
        cls.bill_delivery_orders(quote, invoice)
        return cls._issue(invoice, issue, send_email)

    @classmethod
    @transaction.atomic
    def invoice_delivery_order(
        cls, delivery_order: DeliveryOrder, issue: bool = False, send_email: bool = False
    ) -> SalesInvoice:
        """Invoice one shipment of the order."""
        from invoices.services.delivery_order_service import DeliveryOrderService

        cls._check_invoice_kind(delivery_order.company, delivery_order.client)
        invoice = DeliveryOrderService.create_invoice_from_delivery_orders(
            [delivery_order], delivery_order.company, delivery_order.client, timezone.localdate()
        )
        return cls._issue(invoice, issue, send_email)

    @classmethod
    @transaction.atomic
    def credit(
        cls,
        company: Company,
        client: Client,
        lines: list[CreditNoteLine],
        project_file: ProjectFile | None = None,
        issue: bool = False,
        send_email: bool = False,
        notes: str = "",
    ) -> SalesInvoice:
        """A credit note for returned or refunded ``CreditNoteLine``s, filed under the sale's expediente."""
        from invoices.services.sales_invoice_service import SalesInvoiceService

        cls._check_invoice_kind(company, client)
        credit_note = SalesInvoiceService.create_credit_note(company, client, lines, project_file=project_file, notes=notes)
        return cls._issue(credit_note, issue, send_email)

    @staticmethod
    def bill_delivery_orders(quote: Quote, invoice: SalesInvoice) -> None:
        """Mark the expediente's delivery orders not billed yet as billed by ``invoice``.

        Replacements of lost goods are never billed.
        """
        from invoices.services.delivery_order_service import DeliveryOrderService

        if quote.project_file_id is None:
            return
        unbilled = DeliveryOrderService.get_invoiceable_delivery_orders(quote.project_file).filter(replaces__isnull=True)
        for delivery_order in unbilled:
            delivery_order.sales_invoice = invoice
            delivery_order.save(update_fields=["sales_invoice"])

    @staticmethod
    @transaction.atomic
    def record_payment(
        invoice: SalesInvoice, method: PaymentMethod, reference: str = "", notes: str = ""
    ) -> Payment | None:
        """Settle what is left on an issued invoice with a payment the channel collected."""
        from payments.models import Payment, PaymentAllocation, PaymentCreatedBy, PaymentDirection, PaymentStatus

        amount = invoice.amount_remaining
        if amount.amount <= 0:
            return None
        payment = Payment.objects.create(
            company=invoice.company,
            payment_date=timezone.localdate(),
            amount=amount,
            direction=PaymentDirection.INBOUND,
            method=method,
            status=PaymentStatus.CONFIRMED,
            reference=reference[:100] or None,
            notes=notes or None,
            client=invoice.client,
            created_by=PaymentCreatedBy.AUTO,
        )
        PaymentAllocation.objects.create(
            payment=payment,
            content_type=ContentType.objects.get_for_model(invoice),
            object_id=invoice.pk,
            amount_applied=amount,
        )
        return payment

    @classmethod
    def _check_invoice_kind(cls, company: Company, client: Client) -> None:
        if cls.invoice_kind(company, client) != InvoiceKind.FULL:
            raise ExternalOrderError(_("Simplified invoices are not supported yet."))

    @staticmethod
    def _issue(invoice: SalesInvoice, issue: bool, send_email: bool) -> SalesInvoice:
        """Issue the pro forma as staff would (numbering, tax agency, email), or leave it for review."""
        if not issue:
            return invoice
        from invoices.services.sales_invoice_service import SalesInvoiceService

        return SalesInvoiceService.finalize_invoice(invoice, send_email=send_email)

    @classmethod
    def _cancel_pending_deliveries(cls, quote: Quote) -> None:
        from sales.services.delivery_order_fulfillment_service import DeliveryOrderFulfillmentService

        for delivery_order in cls.delivery_orders(quote).filter(status=DeliveryOrderStatus.DRAFT):
            DeliveryOrderFulfillmentService.cancel(delivery_order)

    @staticmethod
    def _replace_lines(quote: Quote, order: ExternalOrder) -> None:
        from invoices.models import AppliedTax

        line_ct = ContentType.objects.get_for_model(QuoteInventoryItem)
        # Line taxes hang off a generic key, which a line delete doesn't cascade to.
        AppliedTax.objects.filter(content_type=line_ct, object_id__in=quote.inventory_items.values("pk")).delete()
        quote.inventory_items.all().delete()
        for position, external in enumerate(order.lines):
            unit = (external.net_total / external.quantity).quantize(Decimal("0.01")) if external.quantity else Decimal("0")
            line = QuoteInventoryItem(
                quote=quote,
                order=position,
                inventory_item=external.item,
                quantity=external.quantity,
                unit_price=Money(unit, order.currency),
                # Pins the channel's exact net amount; also keeps line-level
                # discount rules away from the line.
                override_line_total=Money(external.net_total, order.currency),
            )
            line._skip_quote_recalc = True
            line.save()
            # The channel's tax replaces the item/client/company defaults.
            line.get_taxes().delete()
            if external.tax_rate:
                AppliedTax.objects.create(
                    content_type=line_ct,
                    object_id=line.pk,
                    tax_type=order.tax_type,
                    selected_percentage=external.tax_rate,
                )
            line._recalc_total_with_tax()
        quote.recalc_totals()
