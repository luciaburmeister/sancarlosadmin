"""Shared builders for sales tests: plain ORM calls, keyword-only arguments.

Taxes are given as ``(tax_type, percentage)`` pairs and stored as AppliedTax
rows in that order.
"""
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType

from company.models import Company
from configurator.models import Configuration
from inventory.models import InventoryItem
from invoices.models import AppliedTax
from sales.models import (
    Client,
    CommissionistAgent,
    DiscountPolicy,
    DiscountRule,
    Quote,
    QuoteInventoryItem,
)

CENT = Decimal("0.01")


def make_company(*, name="Test Co", **kwargs):
    """A company."""
    return Company.objects.create(name=name, **kwargs)


def apply_taxes(obj, *, taxes, **kwargs):
    """AppliedTax rows on ``obj``, ordered as given; ``kwargs`` go on every row."""
    ct = ContentType.objects.get_for_model(obj)
    return [
        AppliedTax.objects.create(
            content_type=ct, object_id=obj.pk, tax_type=tax_type,
            selected_percentage=pct, order=order, **kwargs,
        )
        for order, (tax_type, pct) in enumerate(taxes)
    ]


def make_agent(*, company, name="Agent", **kwargs):
    """A commissionist agent."""
    return CommissionistAgent.objects.create(name=name, company=company, **kwargs)


def make_client(*, company, name="Test Client", taxes=(), **kwargs):
    """A client, with default taxes when ``taxes`` is given."""
    client = Client.objects.create(name=name, company=company, **kwargs)
    apply_taxes(client, taxes=taxes)
    return client


def make_quote(*, company, client=None, project_name="Test Quote", **kwargs):
    """A quote of ``company``."""
    return Quote.objects.create(company=company, client=client, project_name=project_name, **kwargs)


def make_configuration(*, quote=None, subtotal=Decimal("0"), product=None, taxes=(), name=None, **kwargs):
    """A configuration of ``subtotal``; ``taxes`` chain onto its total and tax_breakdown."""
    if quote is not None:
        kwargs.setdefault("company", quote.company)
        kwargs.setdefault("client", quote.client)
    config = Configuration.objects.create(
        quote=quote, product=product, name=name or f"Config {subtotal}",
        subtotal_price=subtotal, total_price=subtotal, **kwargs,
    )
    if not taxes:
        return config
    base = Decimal(str(subtotal))
    ct = ContentType.objects.get_for_model(config)
    for order, (tax_type, pct) in enumerate(taxes):
        AppliedTax.objects.create(
            content_type=ct, object_id=config.pk, tax_type=tax_type, selected_percentage=pct,
            base_amount=base, calculated_amount=(base * pct / Decimal("100")).quantize(CENT), order=order,
        )
    total, breakdown = base, []
    for tax_type, pct in taxes:
        calc = (total * pct / Decimal("100")).quantize(CENT)
        breakdown.append({
            "tax_name": tax_type.tax_name, "percentage": str(pct),
            "base": str(total.quantize(CENT)), "tax_amount": str(calc),
        })
        total = total + calc if tax_type.behavior == "add" else total - calc
    config.tax_breakdown, config.total_price = breakdown, total
    config.save(update_fields=["tax_breakdown", "total_price"])
    return config


def make_inventory_item(*, company, name="Widget", taxes=(), **kwargs):
    """A stock item, with default taxes when ``taxes`` is given."""
    item = InventoryItem.objects.create(name=name, company=company, **kwargs)
    apply_taxes(item, taxes=taxes)
    return item


def make_quote_inventory_line(*, quote, item=None, quantity, unit_price=None, **kwargs):
    """A quote line; without ``item`` a stock item selling at ``unit_price`` is made."""
    if item is None:
        item = make_inventory_item(company=quote.company, selling_price=unit_price)
    if unit_price is not None:
        kwargs["unit_price"] = unit_price
    return QuoteInventoryItem.objects.create(quote=quote, inventory_item=item, quantity=quantity, **kwargs)


def make_discount_rule(*, policy, name, percentage, order=0, **kwargs):
    """A rule of ``policy``; ``percentage`` may be an int, str or Decimal."""
    return DiscountRule.objects.create(
        policy=policy, name=name, percentage=Decimal(str(percentage)), order=order, **kwargs,
    )


def make_discount_policy(*, company, name="Test Policy", rules=(), **kwargs):
    """A policy with ``rules`` (dicts for make_discount_rule; order defaults to position)."""
    policy = DiscountPolicy.objects.create(name=name, company=company, **kwargs)
    for position, rule in enumerate(rules):
        make_discount_rule(policy=policy, **{"order": position, **rule})
    return policy
