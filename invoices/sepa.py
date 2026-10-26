"""
SEPA direct debit file (pain.008.001.02) to collect sales invoices from the
clients' bank accounts: the file is uploaded to the bank, which charges each
client and credits us.

Every collection is booked on its own (BtchBookg=false) with the invoice's
payment reference in its concept, so each credit arrives as a separate bank
movement and the bank matcher (banking/services/transaction_matching)
reconciles it against its invoice like any transfer. A single batch credit
for the whole file would carry no reference and match nothing.
"""
import re
from dataclasses import dataclass
from datetime import date

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from sepaxml import SepaDD
from sepaxml.validation import ValidationError as SepaValidationError

from invoices.models import SalesInvoiceStatus

SCHEMA = "pain.008.001.02"
# Same pattern the schema checks, so a bad IBAN skips its invoice instead of
# failing the whole file
IBAN_RE = re.compile(r"[A-Z]{2}[0-9]{2}[a-zA-Z0-9]{1,30}")


@dataclass
class SkippedInvoice:
    invoice: object
    reason: str


def _compact(value: str) -> str:
    return re.sub(r"\s", "", value or "").upper()


def _mandated_account(client):
    """The client's account to charge: the default one if it has a mandate,
    else the first one that does."""
    accounts = [
        account
        for account in client.client_bank_accounts.all()
        if account.iban and account.sepa_mandate_reference and account.sepa_mandate_signed_on
    ]
    accounts.sort(key=lambda account: (not account.is_default, account.pk))
    return accounts[0] if accounts else None


def check_invoice(invoice, company):
    """``(account, reason)``: the client account the invoice would be
    charged to, or why it cannot be collected (``reason`` set)."""
    account = _mandated_account(invoice.client)
    return account, _skip_reason(invoice, company, account)


def _skip_reason(invoice, company, account):
    if invoice.company_id != company.pk:
        return _("belongs to another company")
    if invoice.status != SalesInvoiceStatus.FINAL:
        return _("is not a final invoice")
    if invoice.currency != "EUR":
        return _("is not in EUR")
    if invoice.amount_remaining_decimal <= 0:
        return _("has nothing left to collect")
    if account is None:
        return _("the client has no bank account with a SEPA mandate")
    if not IBAN_RE.fullmatch(_compact(account.iban)):
        return _("the client's IBAN is not valid")
    return None


def build_direct_debit_xml(invoices, *, creditor_account, collection_date: date):
    """
    Build the SEPA file charging what is left to pay of each invoice on
    ``collection_date`` into ``creditor_account``.

    Returns ``(xml_bytes, skipped)``, ``skipped`` listing the invoices left
    out and why. Raises ValidationError when the creditor data is missing
    or no invoice can be collected.
    """
    company = creditor_account.company
    creditor_id = _compact(company.sepa_creditor_id)
    if not creditor_id:
        raise ValidationError(
            _("%(company)s has no SEPA creditor identifier: set it on the company.")
            % {"company": company}
        )
    creditor_iban = _compact(creditor_account.iban)
    creditor_bic = _compact(creditor_account.swift_bic)
    if not IBAN_RE.fullmatch(creditor_iban) or not creditor_bic:
        raise ValidationError(
            _("The bank account %(account)s needs a valid IBAN and its BIC to collect by SEPA.")
            % {"account": creditor_account}
        )

    sepa = SepaDD(
        {
            "name": company.legal_name or company.name,
            "IBAN": creditor_iban,
            "BIC": creditor_bic,
            "creditor_id": creditor_id,
            "currency": "EUR",
            "batch": False,
            "instrument": "CORE",
        },
        schema=SCHEMA,
    )

    skipped = []
    collected = 0
    for invoice in invoices:
        account, reason = check_invoice(invoice, company)
        if reason:
            skipped.append(SkippedInvoice(invoice, reason))
            continue

        reference = invoice.payment_reference or invoice.display_number
        payment = {
            "name": invoice.client.name,
            "IBAN": _compact(account.iban),
            "amount": int(invoice.amount_remaining_decimal * 100),
            # Banks no longer require FRST on a mandate's first collection
            "type": "RCUR",
            "collection_date": collection_date,
            "mandate_id": account.sepa_mandate_reference.strip(),
            "mandate_date": account.sepa_mandate_signed_on,
            "description": (
                _("Invoice %(number)s %(reference)s")
                % {"number": invoice.display_number, "reference": invoice.payment_reference or ""}
            ).strip(),
            "endtoend_id": reference[:35],
        }
        if account.swift_bic:
            payment["BIC"] = _compact(account.swift_bic)
        sepa.add_payment(payment)
        collected += 1

    if not collected:
        raise ValidationError(_("None of the selected invoices can be collected by SEPA."))

    try:
        return sepa.export(validate=True), skipped
    except SepaValidationError as e:
        raise ValidationError(
            _("The SEPA file is not valid: %(error)s") % {"error": e.__cause__ or e}
        ) from e
