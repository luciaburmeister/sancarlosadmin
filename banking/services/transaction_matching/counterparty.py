"""Counterparty identification from a bank transaction.

Matching tiers (tried in order, first match wins):
1. IBAN match against ProviderBankAccount / ClientBankAccount
2. counterparty_name vs entity names (exact → normalized → contains)
3. Fuzzy match via PostgreSQL trigram similarity (pg_trgm)
"""
import re
import unicodedata

from django.apps import apps
from django.contrib.postgres.search import TrigramSimilarity

from banking.models import BankTransaction
from payments.models import PaymentDirection

# Minimum trigram similarity score to consider a fuzzy match
FUZZY_MIN_SIMILARITY = 0.35

# The best match must be this much better than the runner-up
# to be considered unambiguous (e.g., 0.56 vs 0.37 → gap = 0.19 > 0.10 ✓)
FUZZY_MIN_GAP = 0.10


def find_counterparty(transaction: BankTransaction, direction: str):
    """
    Tries to identify who the counterparty is using a 3-tier strategy.

    Returns the Provider or Client object if found, None otherwise.
    """
    iban = (transaction.counterparty_iban or "").strip()
    counterparty_name = (transaction.counterparty_name or "").strip()
    description = (transaction.description or "").strip()
    company = transaction.company

    if direction == PaymentDirection.OUTBOUND:
        return _find_provider(iban, counterparty_name, description, company)
    else:
        return _find_client(iban, counterparty_name, description, company)


def _find_provider(iban: str, counterparty_name: str, description: str, company):
    """Find a provider by IBAN → name → fuzzy."""
    ProviderBankAccount = apps.get_model("materials", "ProviderBankAccount")
    Provider = apps.get_model("materials", "Provider")

    # Tier 1: IBAN match
    if iban:
        match = (
            ProviderBankAccount.objects
            .filter(iban=iban, company=company)
            .select_related("provider")
            .first()
        )
        if match:
            return match.provider

    # Tier 2: counterparty_name match (exact / normalized / contains)
    if counterparty_name:
        provider = _name_match(Provider, counterparty_name, company)
        if provider:
            return provider

    # Tier 3: fuzzy match on best available text
    search_text = counterparty_name or description
    if search_text:
        return _fuzzy_match(Provider, search_text, company)

    return None


def _find_client(iban: str, counterparty_name: str, description: str, company):
    """Find a client by IBAN → name → fuzzy."""
    ClientBankAccount = apps.get_model("sales", "ClientBankAccount")
    Client = apps.get_model("sales", "Client")

    # Tier 1: IBAN match
    if iban:
        match = (
            ClientBankAccount.objects
            .filter(iban=iban, company=company)
            .select_related("client")
            .first()
        )
        if match:
            return match.client

    # Tier 2: counterparty_name match (exact / normalized / contains)
    if counterparty_name:
        client = _name_match(Client, counterparty_name, company)
        if client:
            return client

    # Tier 3: fuzzy match on best available text
    search_text = counterparty_name or description
    if search_text:
        return _fuzzy_match(Client, search_text, company)

    return None


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

_LEGAL_SUFFIXES = re.compile(
    r"\b(S\.?L\.?U?\.?|S\.?A\.?|S\.?C\.?|S\.?C\.?P\.?|SOCIEDAD LIMITADA"
    r"|SOCIEDAD ANONIMA)\b",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    """
    Normalize a name for comparison:
    - Strip accents
    - Uppercase
    - Remove legal suffixes (SL, SLU, SA, etc.)
    - Remove all non-alphanumeric characters
    - Collapse whitespace
    """
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_text = nfkd.encode("ascii", "ignore").decode("ascii")
    upper = ascii_text.upper()
    cleaned = _LEGAL_SUFFIXES.sub("", upper)
    cleaned = re.sub(r"[^A-Z0-9]+", " ", cleaned).strip()
    return cleaned


# ---------------------------------------------------------------------------
# Matching strategies
# ---------------------------------------------------------------------------

def _name_match(model_class, counterparty_name: str, company):
    """
    Try to match a counterparty_name against Provider/Client names.

    1. Exact case-insensitive match
    2. Normalized comparison (strips legal suffixes, punctuation, accents)
    3. Entity name contained in counterparty_name
    4. Counterparty name contained in entity name

    Only returns if exactly one result (avoids ambiguity).
    """
    # Exact match
    exact = model_class.objects.filter(
        company=company, name__iexact=counterparty_name,
    ).first()
    if exact:
        return exact

    # Normalized comparison
    norm_input = _normalize(counterparty_name)
    if len(norm_input) >= 4:
        entities = model_class.objects.filter(company=company).only("pk", "name")
        matches = [e for e in entities if _normalize(e.name) == norm_input]
        if len(matches) == 1:
            return matches[0]

    # Entity name contained in counterparty_name
    matches = list(
        model_class.objects.filter(company=company)
        .extra(
            where=["UPPER(%s) LIKE UPPER('%%' || name || '%%')"],
            params=[counterparty_name],
        )[:2]
    )
    if len(matches) == 1:
        return matches[0]

    # Counterparty name contained in entity name
    matches = list(
        model_class.objects.filter(
            company=company, name__icontains=counterparty_name,
        )[:2]
    )
    if len(matches) == 1:
        return matches[0]

    return None


def _fuzzy_match(model_class, search_text: str, company):
    """
    Fuzzy match using PostgreSQL trigram similarity (pg_trgm).

    Runs entirely in the database — no Python loops.

    Returns a match only if:
    1. The best score is >= FUZZY_MIN_SIMILARITY
    2. The best score is at least FUZZY_MIN_GAP ahead of the runner-up
    """
    candidates = list(
        model_class.objects
        .filter(company=company)
        .annotate(similarity=TrigramSimilarity("name", search_text))
        .filter(similarity__gte=FUZZY_MIN_SIMILARITY)
        .order_by("-similarity")[:2]
    )

    if not candidates:
        return None

    best = candidates[0]

    # If there's a runner-up, ensure the best is clearly ahead
    if len(candidates) == 2:
        if best.similarity - candidates[1].similarity < FUZZY_MIN_GAP:
            return None  # Too ambiguous

    return best
