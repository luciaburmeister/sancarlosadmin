"""Service for interacting with the Arcopay PSD2 API.

Handles:
- Consent flow (get/renew/cancel)
- Transaction fetching and import into BankTransaction
- Account synchronisation
"""
import hashlib
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import requests
from django.conf import settings
from djmoney.money import Money

from banking.models import BankTransaction, ReconciliationStatus
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
        """Serialize to a plain dict (e.g. for JsonResponse / Celery)."""
        return asdict(self)

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
            "dailyFrequency": 30,
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

    @staticmethod
    def get_token_from_consent(consent_id: str) -> dict:
        """POST /accounts/getToken/ — retrieve a token from a consentId.

        Returns dict with ``consentId``, ``token``, and ``globalPosition``.
        Use this when the token is expired or missing but the consent is still valid.
        """
        resp = requests.post(
            f"{ARCOPAY_API_URL}/accounts/getToken/",
            data={"servicekey": NUEK_SERVICE_KEY, "consentId": consent_id},
            timeout=30,
        )
        if not resp.ok:
            try:
                error_data = resp.json()
            except Exception:
                error_data = resp.text
            logger.error(
                "Arcopay /accounts/getToken/ failed: status=%s, response=%s",
                resp.status_code, error_data,
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

        # Parse response body even on error to get Arcopay's error code
        if not resp.ok:
            try:
                error_data = resp.json()
            except Exception:
                error_data = {"message": resp.text, "code": str(resp.status_code)}
            error_code = error_data.get("code", str(resp.status_code))
            error_msg = error_data.get("message", "Unknown error")
            logger.error(
                "Arcopay /transactions/ failed: status=%s, code=%s, message=%s, token=%s",
                resp.status_code, error_code, error_msg, token[:8],
            )
            raise ArcopayAPIError(error_msg, error_code)

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
        to_create = []
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

            bt = BankTransaction(
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
            to_create.append(bt)

        if to_create:
            created = BankTransaction.objects.bulk_create(to_create)
            from banking.tasks import run_matcher_for_transaction
            for bt in created:
                run_matcher_for_transaction.delay(bt.pk)

        return len(to_create)

    @staticmethod
    def sync_account(bank_account: BankAccount, days_back: int = 90) -> int:
        """Full sync: discover products via GLOBAL, then fetch transactions per product.

        Step 1: Call /transactions/ with GLOBAL to discover products and update metadata.
        Step 2: Call /transactions/ for each product to fetch actual transactions.

        Returns the total number of newly imported transactions.
        """
        token = bank_account.nuek_token
        if not token:
            logger.warning(
                "BankAccount %s has no nuek_token; skipping sync.",
                bank_account.pk,
            )
            return 0

        start_date = date.today() - timedelta(days=days_back)

        # --- Helper to fetch with token refresh on failure ---
        def _fetch(products_param: str) -> list[dict]:
            nonlocal token
            try:
                return ArcopayService.fetch_transactions(
                    token=token, start_date=start_date, products=products_param,
                )
            except (ArcopayAPIError, requests.RequestException) as e:
                if isinstance(e, ArcopayAPIError) and e.code == "T000":
                    logger.warning("Arcopay rate limit for account %s: %s", bank_account.pk, e)
                    raise

                # Try refreshing the token via consent
                consent_id = bank_account.nuek_consent_id
                if not consent_id:
                    raise

                logger.warning("Token failed for account %s, refreshing via consent %s", bank_account.pk, consent_id)
                token_data = ArcopayService.get_token_from_consent(consent_id)
                new_token = token_data.get("token", "")
                if not new_token:
                    raise

                token = new_token
                bank_account.nuek_token = new_token
                bank_account.save(update_fields=["nuek_token"])
                logger.info("Token refreshed for account %s", bank_account.pk)

                return ArcopayService.fetch_transactions(
                    token=token, start_date=start_date, products=products_param,
                )

        # --- Step 1: Discover products via GLOBAL (skip if already known) ---
        if bank_account.nuek_product_id:
            # Products already discovered — skip the GLOBAL call to avoid
            # hitting Arcopay's rate limit unnecessarily.
            product_names = [p.strip() for p in bank_account.nuek_product_id.split(",") if p.strip()]
            logger.info(
                "Account %s: skipping GLOBAL discovery, using known products: %s",
                bank_account.pk, product_names,
            )
        else:
            try:
                global_products = _fetch("GLOBAL")
            except Exception as e:
                logger.error("Failed to discover products for account %s: %s", bank_account.pk, e)
                return 0

            logger.info(
                "Account %s: discovered %d products via GLOBAL",
                bank_account.pk, len(global_products),
            )

            # Upsert metadata and collect product names for step 2
            product_names = []
            for product_data in global_products:
                product_name = product_data.get("product", "")
                if product_name:
                    product_names.append(product_name)

                BankAccount.upsert_from_arcopay(
                    company=bank_account.company,
                    product_data=product_data,
                    token=token,
                    consent_id=bank_account.nuek_consent_id,
                )

                logger.info(
                    "  Product %s — balance=%s, currency=%s",
                    product_name,
                    product_data.get("balance", "?"),
                    product_data.get("currency", "?"),
                )

        # --- Step 2: Fetch transactions for each specific product ---
        total = 0
        for product_name in product_names:
            # ponytail: each product belongs to its own BankAccount
            target_account = BankAccount.objects.filter(
                company=bank_account.company,
                nuek_product_id=product_name,
            ).first() or bank_account

            try:
                product_results = _fetch(product_name)
            except Exception as e:
                logger.error("Failed to fetch transactions for product %s: %s", product_name, e)
                continue

            for product_data in product_results:
                txns = product_data.get("transactions", [])
                logger.info(
                    "  Product %s — %d transactions fetched",
                    product_name, len(txns),
                )
                total += ArcopayService.import_transactions_for_account(
                    target_account, product_data,
                )

                ArcopayService._update_account_balance(target_account)

        logger.info(
            "Imported %d new transactions for BankAccount %s",
            total, bank_account.pk,
        )

        return total

    @staticmethod
    def _update_account_balance(bank_account: BankAccount) -> None:
        """Update last_known_balance from the latest BankTransaction for this specific account.

        Previously this used the ``balance`` field from the Arcopay product
        response, but that value is shared across all accounts under the same
        consent/token, causing every connected account to show the same balance.
        Now we derive the balance from the most recent imported transaction.
        """
        from django.utils import timezone as tz

        latest_txn = (
            BankTransaction.objects
            .filter(bank_account=bank_account)
            .order_by("-transaction_date", "-imported_at")
            .values_list("amount", "amount_currency")
            .first()
        )
        if latest_txn is None:
            return

        amount, currency = latest_txn
        bank_account.last_known_balance = Money(amount, currency)
        bank_account.last_balance_updated_at = tz.now()
        bank_account.save(update_fields=["last_known_balance", "last_known_balance_currency", "last_balance_updated_at"])

    @staticmethod
    def sync_all_connected_accounts(company: Company | None = None) -> "SyncResult":
        """Sync all accounts that have a nuek_token (Arcopay PSD2 token)."""
        from django.db.models import Max

        qs = BankAccount.objects.exclude(nuek_token="").exclude(
            nuek_token__isnull=True
        ).exclude(nuek_product_id="")
        if company:
            qs = qs.filter(company=company)

        # ponytail: order by newest transaction so duplicate product IDs
        # sync only the account that actually has the latest data
        qs = qs.annotate(latest_txn=Max("transactions__transaction_date")).order_by("-latest_txn")

        result = SyncResult()
        seen_products = set()  # ponytail: one sync per (company, product_id)
        for account in qs.select_related("company"):
            key = (account.company_id, account.nuek_product_id)
            if account.nuek_product_id and key in seen_products:
                continue
            if account.nuek_product_id:
                seen_products.add(key)

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
