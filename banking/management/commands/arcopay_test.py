"""Test and debug Arcopay API calls with full response logging.

Usage:
    ./manage.py arcopay_test list_accounts
    ./manage.py arcopay_test get_token --consent-id <UUID>
    ./manage.py arcopay_test transactions --token <TOKEN> [--days 90] [--products GLOBAL]
    ./manage.py arcopay_test consent_info --token <TOKEN>
    ./manage.py arcopay_test sync --account-id <PK>
"""
import json
import logging
from datetime import date, timedelta

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

ARCOPAY_API_URL = getattr(settings, "ARCOPAY_API_URL", "https://apipsd2.arcopay.io")
NUEK_SERVICE_KEY = getattr(settings, "NUEK_SERVICE_KEY", "")


class Command(BaseCommand):
    help = "Test and debug Arcopay PSD2 API calls"

    def add_arguments(self, parser):
        subparsers = parser.add_subparsers(dest="action", help="API action to test")

        # list_accounts
        sub = subparsers.add_parser("list_accounts", help="List all BankAccounts with Arcopay data")
        sub.add_argument("--company-id", type=int, help="Filter by company ID")

        # get_token
        sub = subparsers.add_parser("get_token", help="Call /accounts/getToken/")
        sub.add_argument("--consent-id", required=True, help="Consent UUID")

        # transactions
        sub = subparsers.add_parser("transactions", help="Call /transactions/")
        sub.add_argument("--token", required=True, help="Arcopay token")
        sub.add_argument("--days", type=int, default=90, help="Days back (default: 90)")
        sub.add_argument("--products", default="GLOBAL", help="Products param (default: GLOBAL)")

        # consent_info
        sub = subparsers.add_parser("consent_info", help="Call /consent/info/")
        sub.add_argument("--token", required=True, help="Arcopay token")

        # sync
        sub = subparsers.add_parser("sync", help="Run sync_account for a BankAccount")
        sub.add_argument("--account-id", required=True, type=int, help="BankAccount PK")
        sub.add_argument("--days", type=int, default=90, help="Days back (default: 90)")

    def handle(self, *args, **options):
        action = options.get("action")
        if not action:
            self.stderr.write(self.style.ERROR("Please specify an action. Use --help for options."))
            return

        handler = getattr(self, f"_handle_{action}", None)
        if handler:
            handler(options)
        else:
            self.stderr.write(self.style.ERROR(f"Unknown action: {action}"))

    def _handle_list_accounts(self, options):
        from company.models import BankAccount

        qs = BankAccount.objects.all().order_by("pk")
        if options.get("company_id"):
            qs = qs.filter(company_id=options["company_id"])

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Bank Accounts ===\n"))

        for a in qs:
            connected = bool(a.nuek_token)
            status = self.style.SUCCESS("CONNECTED") if connected else self.style.WARNING("NOT CONNECTED")
            self.stdout.write(f"  [{status}] id={a.pk}  {a.bank_name}")
            self.stdout.write(f"       iban:       {a.iban or '—'}")
            self.stdout.write(f"       token:      {a.nuek_token or '—'}")
            self.stdout.write(f"       consent_id: {a.nuek_consent_id or '—'}")
            self.stdout.write(f"       product_id: {a.nuek_product_id or '—'}")
            self.stdout.write(f"       account_id: {a.nuek_account_id or '—'}")
            self.stdout.write("")

    def _handle_get_token(self, options):
        consent_id = options["consent_id"]
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== /accounts/getToken/ ==="))
        self.stdout.write(f"  consent_id: {consent_id}\n")

        resp = requests.post(
            f"{ARCOPAY_API_URL}/accounts/getToken/",
            data={"servicekey": NUEK_SERVICE_KEY, "consentId": consent_id},
            timeout=30,
        )

        self._print_response(resp)

    def _handle_transactions(self, options):
        token = options["token"]
        days = options["days"]
        products = options["products"]
        start_date = (date.today() - timedelta(days=days)).strftime("%d-%m-%Y")

        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== /transactions/ ==="))
        self.stdout.write(f"  token:     {token}")
        self.stdout.write(f"  startDate: {start_date}")
        self.stdout.write(f"  products:  {products}\n")

        resp = requests.post(
            f"{ARCOPAY_API_URL}/transactions/",
            data={
                "servicekey": NUEK_SERVICE_KEY,
                "token": token,
                "startDate": start_date,
                "products": products,
            },
            timeout=60,
        )

        self._print_response(resp)

    def _handle_consent_info(self, options):
        token = options["token"]
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== /consent/info/ ==="))
        self.stdout.write(f"  token: {token}\n")

        resp = requests.post(
            f"{ARCOPAY_API_URL}/consent/info/",
            data={"servicekey": NUEK_SERVICE_KEY, "token": token},
            timeout=30,
        )

        self._print_response(resp)

    def _handle_sync(self, options):
        from company.models import BankAccount
        from banking.services.arcopay_import import ArcopayService

        account_id = options["account_id"]
        days = options["days"]

        try:
            account = BankAccount.objects.get(pk=account_id)
        except BankAccount.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"BankAccount {account_id} not found"))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== Sync Account {account_id} ==="))
        self.stdout.write(f"  bank:       {account.bank_name}")
        self.stdout.write(f"  iban:       {account.iban}")
        self.stdout.write(f"  token:      {account.nuek_token}")
        self.stdout.write(f"  consent_id: {account.nuek_consent_id}")
        self.stdout.write(f"  product_id: {account.nuek_product_id}\n")

        count = ArcopayService.sync_account(account, days_back=days)
        self.stdout.write(self.style.SUCCESS(f"\n  Imported {count} new transactions"))

    def _print_response(self, resp):
        """Pretty-print an HTTP response."""
        status_style = self.style.SUCCESS if resp.ok else self.style.ERROR
        self.stdout.write(f"  Status: {status_style(str(resp.status_code))}")
        self.stdout.write(f"  Headers:")
        for key in ("Debug-id", "Accept-version", "Content-Type"):
            if key in resp.headers:
                self.stdout.write(f"    {key}: {resp.headers[key]}")

        try:
            data = resp.json()
            # If globalPosition is a JSON string, parse it for readability
            if isinstance(data, dict) and "globalPosition" in data:
                gp = data["globalPosition"]
                if isinstance(gp, str):
                    try:
                        data["globalPosition"] = json.loads(gp)
                    except json.JSONDecodeError:
                        pass
            self.stdout.write(f"  Body:")
            self.stdout.write(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception:
            self.stdout.write(f"  Body (raw): {resp.text[:2000]}")

        self.stdout.write("")
