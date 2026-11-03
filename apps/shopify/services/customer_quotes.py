"""
A store's quotes area: the quotes a signed-in customer finds on the website,
their PDF, and accepting one sent to them.

Only customers staff gave web store access (``ShopifyCustomerLink.web_store_access``)
have one, and they see their client's quotes. Which of them is the store's
``customer_quotes`` choice, one ``CustomerQuoteScope`` per choice. The
storefront reads them through the app proxy as ``CustomerQuoteSummary``.
Accepting is the ERP's normal acceptance: the quote is filed under a new
expediente, as the admin wizard does. An accepted quote may then be paid
online (``ShopifyQuotePaymentService``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils.translation import gettext as _

from apps.shopify.models import CustomerQuotesMode, ShopifyCustomerLink, ShopifyStore
from apps.shopify.services.quote_payments import ShopifyQuotePaymentService
from management.services.quote_acceptance import QuoteAcceptanceService
from sales.models import Quote, QuoteStatus

if TYPE_CHECKING:
    from sales.models import Client


class CustomerQuoteError(Exception):
    """The customer can't do this with the quote; the message says why."""


class CustomerQuoteState(StrEnum):
    """Where a quote stands for the customer, as the storefront shows it."""

    REQUESTED = "requested"  # being prepared: no price yet
    SENT = "sent"  # waiting for the customer
    EXPIRED = "expired"  # sent, but no longer valid
    ACCEPTED = "accepted"
    PAID = "paid"  # accepted and paid online


@dataclass(frozen=True)
class CustomerQuoteSummary:
    id: int
    name: str
    date: str | None
    state: str
    total: str | None  # only once priced, i.e. sent
    currency: str | None
    expires_at: str | None
    can_accept: bool
    can_pay: bool

    @classmethod
    def of(cls, store: ShopifyStore, quote: Quote) -> CustomerQuoteSummary:
        priced = quote.status != QuoteStatus.DRAFT
        return cls(
            id=quote.pk,
            name=quote.project_name,
            date=quote.date.isoformat() if quote.date else None,
            state=CustomerQuoteService.state(quote),
            total=str(quote.total_price.amount) if priced and quote.total_price is not None else None,
            currency=str(quote.total_price.currency) if priced and quote.total_price is not None else None,
            expires_at=quote.expires_at.isoformat() if quote.expires_at else None,
            can_accept=quote.can_be_accepted_by_client,
            can_pay=ShopifyQuotePaymentService.can_pay(store, quote),
        )

    def as_dict(self) -> dict:
        return asdict(self)


class CustomerQuoteScope(ABC):
    """Which of a client's quotes the store's quotes area shows."""

    @abstractmethod
    def quotes(self, store: ShopifyStore, client: Client) -> QuerySet[Quote]:
        """The client's quotes to show, of any status; rejected ones are left out afterwards."""


class RequestedQuotesScope(CustomerQuoteScope):
    """The quotes the client's customers requested on this store."""

    def quotes(self, store: ShopifyStore, client: Client) -> QuerySet[Quote]:
        return Quote.objects.filter(company=store.company, client=client, shopify_quote__store=store)


class AllQuotesScope(CustomerQuoteScope):
    """Every quote sent to the client, and those requested on this store while still being prepared."""

    def quotes(self, store: ShopifyStore, client: Client) -> QuerySet[Quote]:
        return Quote.objects.filter(company=store.company, client=client).filter(
            Q(status__in=[QuoteStatus.SENT, QuoteStatus.ACCEPTED]) | Q(shopify_quote__store=store)
        )


class CustomerQuoteService:
    SCOPES: dict[str, CustomerQuoteScope] = {
        CustomerQuotesMode.REQUESTED: RequestedQuotesScope(),
        CustomerQuotesMode.ALL: AllQuotesScope(),
    }

    @staticmethod
    def client_of(store: ShopifyStore, customer_id: str) -> Client | None:
        """The client whose quotes the customer may see: only with web store access."""
        link = (
            ShopifyCustomerLink.objects.filter(store=store, customer_id=customer_id, web_store_access=True)
            .select_related("client")
            .first()
        )
        return link.client if link is not None else None

    @classmethod
    def quotes(cls, store: ShopifyStore, client: Client) -> QuerySet[Quote]:
        """The quotes the store's quotes area shows the client, newest first."""
        return (
            cls.SCOPES[store.customer_quotes]
            .quotes(store, client)
            .exclude(status=QuoteStatus.REJECTED)
            .select_related("shopify_payment__order", "shopify_order")
            # Whether each accepted one can be paid online, without a query per quote.
            .with_project_file_activity()
            .order_by("-date", "-pk")
        )

    @classmethod
    def accepted_quote(cls, store: ShopifyStore, client: Client, quote_id: int) -> Quote:
        """One of the client's accepted quotes; ``Quote.DoesNotExist`` otherwise."""
        return cls.quotes(store, client).filter(status=QuoteStatus.ACCEPTED).get(pk=quote_id)

    @classmethod
    def priced_quote(cls, store: ShopifyStore, client: Client, quote_id: int) -> Quote:
        """One of the client's quotes that has a price (sent or accepted); ``Quote.DoesNotExist`` otherwise."""
        return cls.quotes(store, client).exclude(status=QuoteStatus.DRAFT).get(pk=quote_id)

    @staticmethod
    def state(quote: Quote) -> CustomerQuoteState:
        if quote.status == QuoteStatus.ACCEPTED:
            payment = getattr(quote, "shopify_payment", None)
            return CustomerQuoteState.PAID if payment is not None and payment.is_paid else CustomerQuoteState.ACCEPTED
        if quote.status == QuoteStatus.DRAFT:
            return CustomerQuoteState.REQUESTED
        return CustomerQuoteState.EXPIRED if quote.is_expired else CustomerQuoteState.SENT

    @classmethod
    @transaction.atomic
    def accept(cls, store: ShopifyStore, client: Client, quote_id: int) -> Quote:
        """The client accepts a quote sent to them; ``Quote.DoesNotExist`` when it isn't theirs to see."""
        quote = cls.quotes(store, client).select_related(None).select_for_update(of=("self",)).get(pk=quote_id)
        if quote.status == QuoteStatus.ACCEPTED:
            raise CustomerQuoteError(_("This quote is already accepted."))
        if not quote.can_be_accepted_by_client:
            raise CustomerQuoteError(_("This quote can no longer be accepted. Ask us for an updated one."))
        QuoteAcceptanceService.accept(quote)
        return quote
