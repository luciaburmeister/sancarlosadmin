"""Service for interacting with the Arcopay PSD2 API.

Handles:
- Consent flow (get/renew/cancel)
- Transaction fetching and import into BankTransaction
- Account synchronisation
"""
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import requests
from django.conf import settings
from django.db import transaction as db_transaction
from djmoney.money import Money

from banking.models import BankTransaction, ReconciliationStatus
from banking.services.transaction_matching import run_matcher
from company.models import BankAccount, Company

logger = logging.getLogger(__name__)

ARCOPAY_API_URL = getattr(settings, "ARCOPAY_API_URL", "https://apipsd2.arcopay.io")
NUEK_SERVICE_KEY = getattr(settings, "NUEK_SERVICE_KEY", "")


@dataclass
class SyncAccountResult:
    """Result of syncing a single bank account."""
    id: int
    bank: str
    imported: int


@dataclass
class SyncAccountError:
    """Error from syncing a single bank account."""
    id: int
    error: str


@dataclass
class SyncResult:
    """Aggregated result of syncing all connected accounts."""
    synced: int = 0
    accounts: list[SyncAccountResult] = field(default_factory=list)
    errors: list[SyncAccountError] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize to a plain dict (e.g. for JsonResponse)."""
        return {
            "synced": self.synced,
            "accounts": [
                {"id": a.id, "bank": a.bank, "imported": a.imported}
                for a in self.accounts
            ],
            "errors": [
                {"id": e.id, "error": e.error}
                for e in self.errors
            ],
        }

class ArcopayService:
    """Thin wrapper around the Arcopay PSD2 REST API."""

    # ------------------------------------------------------------------
    # Bank listing
    # ------------------------------------------------------------------

    @staticmethod
    def list_supported_banks(country_code: str = "ES") -> list[dict]:
        """GET /listOfSupportedBanks/ — fetch available banks.

        Returns a list of dicts with keys: service, fullname, image, imageSVG,
        swift, countryCode, paymentsSupported.
        """
        params = {}
        if country_code:
            params["countryCode"] = country_code

        resp = requests.get(
            f"{ARCOPAY_API_URL}/listOfSupportedBanks/",
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "code" in data:
            raise ArcopayAPIError(data.get("message", "Unknown error"), data.get("code", "1"))
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------
    # Consent helpers
    # ------------------------------------------------------------------


    @staticmethod
    def get_consent(
        bank_service: str,
        callback_url: str,
        callback_error_url: str = "",
        valid_until: date | None = None,
        redirect_url: str = "",
    ) -> dict:
        """Call POST /consent/get/ to start the PSD2 consent flow.

        Returns dict with ``follow`` (URL for user) and ``consentId``.
        """
        if valid_until is None:
            valid_until = date.today() + timedelta(days=180)

        payload = {
            "servicekey": NUEK_SERVICE_KEY,
            "service": bank_service,
            "grantType": "read",
            "validUntil": valid_until.strftime("%d-%m-%Y"),
            "dailyFrequency": 4,
            "yourConsentCallback": callback_url,
        }
        if callback_error_url:
            payload["yourConsentCallbackError"] = callback_error_url
        if redirect_url:
            payload["urlRedirect"] = redirect_url

        resp = requests.post(
            f"{ARCOPAY_API_URL}/consent/get/",
            data=payload,
            timeout=30,
        )

        # Capture the response body before raising so we get Arcopay's error message
        if not resp.ok:
            try:
                error_data = resp.json()
            except Exception:
                error_data = resp.text
            logger.error(
                "Arcopay /consent/get/ failed: status=%s, payload=%s, response=%s",
                resp.status_code, payload, error_data,
            )
            error_msg = error_data.get("message", str(error_data)) if isinstance(error_data, dict) else str(error_data)
            raise ArcopayAPIError(error_msg, str(resp.status_code))

        return resp.json()

    @staticmethod
    def get_consent_info(token: str) -> dict:
        """POST /consent/info/ — get consent details."""
        resp = requests.post(
            f"{ARCOPAY_API_URL}/consent/info/",
            data={"servicekey": NUEK_SERVICE_KEY, "token": token},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def renew_consent(token: str, valid_until: date | None = None) -> dict:
        """POST /consent/renew/"""
        payload = {"servicekey": NUEK_SERVICE_KEY, "token": token}
        if valid_until:
            payload["validUntil"] = valid_until.strftime("%d-%m-%Y")
        resp = requests.post(
            f"{ARCOPAY_API_URL}/consent/renew/",
            data=payload,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def cancel_consent(token: str) -> dict:
        """POST /consent/cancel/"""
        resp = requests.post(
            f"{ARCOPAY_API_URL}/consent/cancel/",
            data={"servicekey": NUEK_SERVICE_KEY, "token": token},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Transaction fetching
    # ------------------------------------------------------------------

    @staticmethod
    def fetch_transactions(
        token: str,
        start_date: date,
        products: str = "GLOBAL",
    ) -> list[dict]:
        """POST /transactions/ — fetch bank movements.

        First call should use ``products="GLOBAL"`` to discover accounts.
        Subsequent calls can specify a comma-separated list of product names
        (IBANs).

        Returns a list of product dicts, each containing ``product``,
        ``type``, ``balance``, ``countable_balance``, ``arrangement``,
        ``currency``, and ``transactions`` (list).
        """
        resp = requests.post(
            f"{ARCOPAY_API_URL}/transactions/",
            data={
                "servicekey": NUEK_SERVICE_KEY,
                "token": token,
                "startDate": start_date.strftime("%d-%m-%Y"),
                "products": products,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        # The API returns either a list or a single error dict
        if isinstance(data, dict) and "code" in data:
            raise ArcopayAPIError(data.get("message", "Unknown error"), data.get("code", "1"))
        return data if isinstance(data, list) else [data]

    # ------------------------------------------------------------------
    # Import logic
    # ------------------------------------------------------------------

    @staticmethod
    def _build_external_id(product: str, txn: dict) -> str:
        """Build a unique external_id for deduplication.

        Arcopay guarantees ``transactionId`` is unique per product,
        so we combine ``product + transactionId``.
        """
        txn_id = txn.get("transactionId", "")
        raw = f"{product}:{txn_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:64]

    @staticmethod
    def import_transactions_for_account(
        bank_account: BankAccount,
        product_data: dict,
    ) -> int:
        """Import transactions from one Arcopay product response into BankTransaction.

        Deduplicates by ``external_id``.  Runs the matcher on every
        newly created transaction.

        Returns the count of newly imported transactions.
        """
        product_name = product_data.get("product", "")
        raw_transactions = product_data.get("transactions", [])
        if not raw_transactions:
            return 0

        # Gather existing external_ids for fast lookup
        candidates = [
            ArcopayService._build_external_id(product_name, txn)
            for txn in raw_transactions
        ]
        existing = set(
            BankTransaction.objects.filter(
                external_id__in=candidates,
            ).values_list("external_id", flat=True)
        )

        created_count = 0
        for txn in raw_transactions:
            ext_id = ArcopayService._build_external_id(product_name, txn)
            if ext_id in existing:
                continue

            # Parse amount — positive = credit, negative = debit
            try:
                amount = Decimal(str(txn.get("amount", "0")))
            except Exception:
                amount = Decimal("0")

            # Parse date (dd-mm-yyyy format from Arcopay)
            txn_date_str = txn.get("date", "") or txn.get("date2", "")
            try:
                parts = txn_date_str.split("-")
                txn_date = date(int(parts[2]), int(parts[1]), int(parts[0]))
            except (IndexError, ValueError):
                txn_date = date.today()

            description = txn.get("description", "")
            creditor = txn.get("creditorName", "")
            debtor = txn.get("debtorName", "")
            counterparty_name = creditor or debtor
            counterparty_iban = txn.get("counterpartyIBAN", "") or txn.get("iban", "") or ""

            currency = product_data.get("currency", "EUR") or "EUR"

            with db_transaction.atomic():
                bt = BankTransaction.objects.create(
                    company=bank_account.company,
                    bank_account=bank_account,
                    external_id=ext_id,
                    transaction_date=txn_date,
                    amount=Money(amount, currency),
                    currency=currency,
                    description=description,
                    counterparty_name=counterparty_name,
                    counterparty_iban=counterparty_iban,
                    raw_data=txn,
                    reconciliation_status=ReconciliationStatus.UNRECONCILED,
                )
                # Run the auto-matcher
                try:
                    run_matcher(bt)
                except Exception:
                    logger.exception("Matcher failed for BankTransaction %s", bt.pk)

            created_count += 1

        return created_count

    @staticmethod
    def sync_account(bank_account: BankAccount, days_back: int = 90) -> int:
        """Full sync: fetch + import for a single account.

        Returns the total number of newly imported transactions.
        """
        token = bank_account.nuek_account_id
        if not token:
            logger.warning(
                "BankAccount %s has no nuek_account_id (token); skipping sync.",
                bank_account.pk,
            )
            return 0

        start_date = date.today() - timedelta(days=days_back)
        try:
            products = ArcopayService.fetch_transactions(
                token=token,
                start_date=start_date,
                products="GLOBAL",
            )
        except ArcopayAPIError as e:
            logger.error("Arcopay API error syncing account %s: %s", bank_account.pk, e)
            return 0
        except requests.RequestException as e:
            logger.error("HTTP error syncing account %s: %s", bank_account.pk, e)
            return 0

        logger.info(
            "Arcopay /transactions/ response for account %s: %d products returned",
            bank_account.pk,
            len(products),
        )
        for i, prod in enumerate(products):
            txns = prod.get("transactions", [])
            logger.info(
                "  Product %d: %s — %d transactions, balance=%s",
                i,
                prod.get("product", "?"),
                len(txns),
                prod.get("balance", "?"),
            )

        total = 0
        for product_data in products:
            total += ArcopayService.import_transactions_for_account(
                bank_account, product_data
            )

        logger.info(
            "Imported %d new transactions for BankAccount %s",
            total,
            bank_account.pk,
        )

        return total

    @staticmethod
    def sync_all_connected_accounts(company: Company | None = None) -> "SyncResult":
        """Sync all accounts that have a nuek_account_id (Arcopay token)."""
        qs = BankAccount.objects.exclude(nuek_account_id="").exclude(
            nuek_account_id__isnull=True
        )
        if company:
            qs = qs.filter(company=company)

        result = SyncResult()
        for account in qs.select_related("company"):
            try:
                count = ArcopayService.sync_account(account)
                result.synced += count
                result.accounts.append(
                    SyncAccountResult(
                        id=account.pk,
                        bank=account.bank_name,
                        imported=count,
                    )
                )
            except Exception as e:
                logger.exception("Error syncing account %s", account.pk)
                result.errors.append(
                    SyncAccountError(id=account.pk, error=str(e))
                )

        return result

    @staticmethod
    def store_accounts_from_token(
        token: str,
        company: Company,
        consent_id: str = "",
    ) -> list[BankAccount]:
        """Fetch accounts via /transactions/ and upsert BankAccounts.

        Used when the callback didn't create the accounts (e.g. 402 consent
        already valid) and we need to create them from the token directly.

        Returns the list of upserted BankAccount instances.
        """
        products = ArcopayService.fetch_transactions(
            token=token,
            start_date=date.today() - timedelta(days=90),
            products="GLOBAL",
        )
        accounts = []
        for product_data in products:
            account, created = BankAccount.upsert_from_arcopay(
                company=company,
                product_data=product_data,
                token=token,
                consent_id=consent_id,
            )
            ArcopayService.import_transactions_for_account(account, product_data)
            accounts.append(account)
        return accounts

    # ------------------------------------------------------------------
    # Process consent callback
    # ------------------------------------------------------------------

    @staticmethod
    def process_consent_callback(payload: dict, company: Company) -> dict:
        """Handle the POST callback from Arcopay after consent is granted.

        Payload schema (consentGetCallback):
            {
                "token": "...",
                "consentId": "...",
                "globalPosition": [{product, type, balance, ...}, ...]
            }

        For each product in globalPosition, creates or updates a
        BankAccount and imports initial transactions.
        """
        token = payload.get("token", "")
        consent_id = payload.get("consentId", "")
        global_position = payload.get("globalPosition", [])

        if not token:
            raise ValueError("Missing token in consent callback")

        if isinstance(global_position, dict):
            global_position = [global_position]

        accounts_processed = []

        for product_data in global_position:
            bank_account, created = BankAccount.upsert_from_arcopay(
                company=company,
                product_data=product_data,
                token=token,
                consent_id=consent_id,
            )

            # Import any initial transactions included in globalPosition
            imported = ArcopayService.import_transactions_for_account(
                bank_account, product_data
            )

            accounts_processed.append({
                "bank_account_id": bank_account.pk,
                "iban": bank_account.iban,
                "imported_transactions": imported,
            })

        return {
            "consent_id": consent_id,
            "token": token,
            "accounts": accounts_processed,
        }


class ArcopayAPIError(Exception):
    """Raised when the Arcopay API returns an error response."""

    def __init__(self, message: str, code: str = "1"):
        self.code = code
        super().__init__(f"[{code}] {message}")
