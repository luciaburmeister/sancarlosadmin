"""Repair clients that sit in one company while another company's documents
point at them.

The document is right: a Fistex invoice is a Fistex invoice. What is wrong is
which company owns the client record. So for every client the repair asks who
actually uses it — invoices, quotes, orders, delivery orders, expedientes,
configurations, assets… — and decides:

* nobody from its own company uses it, and one other company does -> the
  client is moved to that company, sub-records included;
* its own company uses it too (or several other companies do) -> a copy of
  the client is created in each of the other companies and that company's
  documents are repointed to the copy, so no company loses its history;
* the other company already has a client with the same tax identifier -> its
  documents are repointed to that existing client instead of creating a
  duplicate, since a tax identifier is unique per company.

Addresses, bank accounts and contacts travel with the client: re-homed on a
move, duplicated on a copy. Delivery orders that pointed at an address of the
original client are repointed to the copy of that address. Everything that is
company-scoped and would leak across the border (sale zone, agent, our bank
account, discount policies, accepted terms) is dropped from the moved or
copied client.
"""
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from djmoney.money import Money

if TYPE_CHECKING:
    from sales.models import Client

# Sub-records that belong to the client itself rather than being a company
# using the client. They follow it around instead of voting on where it lives.
SUBRECORD_MODELS = {
    "sales.ClientAddress",
    "sales.ClientBankAccount",
    "sales.ClientContact",
}

# Client fields pointing at company-scoped records, which cannot survive a
# move or a copy into a different company.
COMPANY_SCOPED_FIELDS = ("sale_zone", "agent", "our_bank_account")

MOVE = "move"
COPY = "copy"
MERGE = "merge"


@dataclass
class Action:
    """What to do with one company's documents for one client."""
    kind: str
    company_id: int
    records: int
    target_id: int | None = None      # existing twin, for MERGE
    target_label: str = ""

    def __str__(self):
        if self.kind == MOVE:
            return f"move client to company {self.company_id} ({self.records} records)"
        if self.kind == MERGE:
            return (
                f"merge {self.records} records of company {self.company_id} "
                f"into existing client #{self.target_id} ({self.target_label})"
            )
        return f"copy client into company {self.company_id} and move {self.records} records"


@dataclass
class ClientPlan:
    client_id: int
    label: str
    tax_identifier: str
    company_id: int | None
    usage: Counter                    # company_id -> number of records
    actions: list[Action] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def usage_relations():
    """(model, client field name) for every relation that means "this company
    uses this client" — everything pointing at Client except its own
    sub-records and anything not company-scoped."""
    from sales.models import Client

    relations = []
    for rel in Client._meta.related_objects:
        model = rel.related_model
        if model._meta.label in SUBRECORD_MODELS:
            continue
        if not any(f.name == "company" for f in model._meta.concrete_fields):
            continue
        relations.append((model, rel.field.name))
    return relations


def find_mismatched_client_ids(company_ids=None):
    """Clients with at least one document belonging to a different company."""
    from django.db.models import F

    ids = set()
    for model, field_name in usage_relations():
        qs = model.objects.filter(
            **{f"{field_name}_id__isnull": False, "company_id__isnull": False}
        ).exclude(company_id=F(f"{field_name}__company_id"))
        if company_ids:
            qs = qs.filter(**{f"{field_name}__company_id__in": company_ids})
        ids.update(qs.values_list(f"{field_name}_id", flat=True))
    return sorted(ids)


def collect_usage(client_ids):
    """client_id -> Counter of company_id -> number of records using it."""
    usage = {client_id: Counter() for client_id in client_ids}
    if not client_ids:
        return usage
    for model, field_name in usage_relations():
        rows = model.objects.filter(**{f"{field_name}_id__in": client_ids}).values_list(
            f"{field_name}_id", "company_id"
        )
        for client_id, company_id in rows:
            usage[client_id][company_id] += 1
    return usage


def find_twin(client, company_id):
    """The company's own client record for the same tax identifier, if any."""
    from sales.models import Client

    tax_identifier = (client.tax_identifier or "").strip()
    if not tax_identifier:
        return None
    return (
        Client.objects.filter(
            company_id=company_id, tax_identifier__iexact=tax_identifier
        )
        .exclude(pk=client.pk)
        .first()
    )


def build_plans(client_ids):
    from sales.models import Client

    clients = Client.objects.filter(pk__in=client_ids).order_by("company_id", "pk")
    usage = collect_usage(list(clients.values_list("pk", flat=True)))

    plans = []
    for client in clients:
        counts = usage.get(client.pk, Counter())
        foreign = {
            company_id: records
            for company_id, records in counts.items()
            if company_id is not None and company_id != client.company_id
        }
        if not foreign:
            continue

        plan = ClientPlan(
            client_id=client.pk,
            label=str(client),
            tax_identifier=client.tax_identifier,
            company_id=client.company_id,
            usage=counts,
        )
        if counts.get(None):
            plan.notes.append(
                f"{counts[None]} records without a company stay with the original client"
            )

        # Busiest company first, so a move lands where most of the history is.
        ordered = sorted(foreign.items(), key=lambda item: (-item[1], item[0]))
        twins = {company_id: find_twin(client, company_id) for company_id, _ in ordered}
        own_use = client.company_id is not None and counts.get(client.company_id, 0) > 0
        move_to = None
        if not own_use:
            move_to = next(
                (company_id for company_id, _ in ordered if twins[company_id] is None),
                None,
            )

        for company_id, records in ordered:
            twin = twins[company_id]
            if twin is not None:
                plan.actions.append(
                    Action(
                        MERGE,
                        company_id,
                        records,
                        target_id=twin.pk,
                        target_label=str(twin),
                    )
                )
            elif company_id == move_to:
                plan.actions.append(Action(MOVE, company_id, records))
            else:
                plan.actions.append(Action(COPY, company_id, records))

        if not own_use and move_to is None:
            plan.notes.append(
                "the original client keeps no documents; review whether it should be deleted"
            )
        plans.append(plan)
    return plans


def apply_plan(plan):
    """Run one client's plan. Call inside a transaction."""
    from sales.models import Client

    client = Client.objects.get(pk=plan.client_id)
    # A move rewrites the client itself, so run it after the copies have been
    # taken from the untouched original.
    for action in sorted(plan.actions, key=lambda a: a.kind == MOVE):
        if action.kind == MOVE:
            _move_client(client, action.company_id)
            continue
        if action.kind == MERGE:
            target = Client.objects.get(pk=action.target_id)
            addresses = {}
        else:
            target, addresses = _copy_client(client, action.company_id)
        _repoint(client, target, action.company_id, addresses)


def _move_client(client: "Client", company_id: int) -> "Client":
    from sales.models import ClientAddress, ClientBankAccount

    client.company_id = company_id
    for field_name in COMPANY_SCOPED_FIELDS:
        related = getattr(client, field_name, None)
        if related is not None and related.company_id != company_id:
            setattr(client, field_name, None)
    client.save()

    stale_policies = [
        policy
        for policy in client.discount_policies.all()
        if policy.company_id != company_id
    ]
    if stale_policies:
        client.discount_policies.remove(*stale_policies)
        client.discount_policies_changed()

    ClientAddress.objects.filter(client=client).update(company_id=company_id)
    ClientBankAccount.objects.filter(client=client).update(company_id=company_id)
    return client


def _copy_client(client, company_id):
    """A twin of the client in the other company, with its sub-records.
    Returns (copy, {original address id: copied address})."""
    copy = _clone(
        client,
        company_id=company_id,
        sale_zone_id=None,
        agent_id=None,
        our_bank_account_id=None,
        default_client_bank_account_id=None,
        terms_accepted_at=None,
        terms_signature=None,
        accepted_terms_company_id=None,
        accepted_terms_text="",
    )
    # Risk is exposure towards one company; the copy starts with none.
    copy.current_risk = Money(0, client.current_risk_currency)
    copy.save()

    addresses = {}
    for address in client.addresses.all():
        addresses[address.pk] = _clone(
            address, client_id=copy.pk, company_id=company_id, save=True
        )

    accounts = {}
    for account in client.client_bank_accounts.all():
        accounts[account.pk] = _clone(
            account, client_id=copy.pk, company_id=company_id, save=True
        )

    for contact in client.contacts.all():
        copied_address = addresses.get(contact.address_id)
        _clone(
            contact,
            client_id=copy.pk,
            address_id=copied_address.pk if copied_address else None,
            save=True,
        )

    copied_default = accounts.get(client.default_client_bank_account_id)
    if copied_default is not None:
        copy.default_client_bank_account = copied_default
        copy.save(update_fields=["default_client_bank_account"])

    return copy, addresses


def _repoint(client, target, company_id, addresses):
    """Hand that company's documents over to the target client."""
    from sales.models import DeliveryOrder

    moved = 0
    for model, field_name in usage_relations():
        moved += model.objects.filter(
            **{field_name: client, "company_id": company_id}
        ).update(**{f"{field_name}_id": target.pk})

    # Delivery orders now belong to the target but may still point at an
    # address of the original client.
    copied = dict(addresses)
    delivery_orders = DeliveryOrder.objects.filter(
        client=target, company_id=company_id, delivery_address__isnull=False
    ).select_related("delivery_address")
    for delivery_order in delivery_orders:
        address = delivery_order.delivery_address
        if address.client_id == target.pk:
            continue
        if address.pk not in copied:
            copied[address.pk] = _clone(
                address,
                client_id=target.pk,
                company_id=company_id,
                is_default=False,
                save=True,
            )
        DeliveryOrder.objects.filter(pk=delivery_order.pk).update(
            delivery_address=copied[address.pk]
        )
    return moved


def _clone(instance, save=False, **overrides):
    model = type(instance)
    data = {
        f.attname: getattr(instance, f.attname)
        for f in model._meta.concrete_fields
        if not f.primary_key
    }
    data.update(overrides)
    clone = model(**data)
    if save:
        clone.save()
    return clone
