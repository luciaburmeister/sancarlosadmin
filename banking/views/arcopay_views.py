"""Arcopay admin views — PSD2 consent flow via the Arcopay REST API.

Flow:
    1. ConnectBankView — shows list of supported banks + company selector
    2. InitiateConsentView — calls /consent/get/, redirects to bank auth
    3. ConsentDoneView — user returns here after bank auth → redirects to BankAccount
    4. SyncAccountsView — manually trigger transaction sync
"""
import logging

from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from unfold.views import UnfoldModelAdminViewMixin

from banking.services.arcopay_import import ArcopayService
from company.models import Company,BankAccount
from core.admin.company_access import get_user_companies

logger = logging.getLogger(__name__)


class ConnectBankView(UnfoldModelAdminViewMixin, TemplateView):
    """Step 1: Show list of supported banks + company selector."""

    title = _("Connect Bank Account")
    permission_required = ("banking.view_banktransaction",)
    template_name = "admin/banking/connect_bank.html"

    def get(self, request, *args, **kwargs):
        self.companies = get_user_companies(request.user)
        if not self.companies:
            messages.error(request, _("No active company found."))
            return redirect("admin:banking_banktransaction_changelist")
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        banks = []
        error_message = ""
        try:
            banks = ArcopayService.list_supported_banks(country_code="ES")
        except Exception as e:
            logger.exception("Failed to fetch bank list from Arcopay")
            error_message = str(e)

        context.update({
            "title": self.title,
            "banks": banks,
            "companies": self.companies,
            "error_message": error_message,
            "initiate_url": reverse("admin:banking_initiate_consent"),
        })
        return context


class InitiateConsentView(UnfoldModelAdminViewMixin, View):
    """Step 2: User selected a bank + company → call /consent/get/ → redirect."""

    title = _("Initiate Bank Connection")
    permission_required = ("banking.view_banktransaction",)

    def post(self, request):
        # Validate company_id from the form
        company_id = request.POST.get("company_id", "")
        if not company_id:
            messages.error(request, _("Please select a company."))
            return redirect("admin:banking_connect_bank")

        user_companies = get_user_companies(request.user)
        company = next((c for c in user_companies if str(c.pk) == str(company_id)), None)
        if not company:
            messages.error(request, _("You don't have access to that company."))
            return redirect("admin:banking_connect_bank")

        bank_service = request.POST.get("bank_service", "")
        if not bank_service:
            messages.error(request, _("Please select a bank."))
            return redirect("admin:banking_connect_bank")

        # Build callback + redirect URLs
        backend_url = settings.BACKEND_URL.rstrip("/")
        callback_url = f"{backend_url}/api/banking/arcopay/callback/?company_id={company.pk}"
        callback_error_url = f"{backend_url}/api/banking/arcopay/callback-error/?company_id={company.pk}"

        redirect_url = request.build_absolute_uri(
            reverse("admin:banking_consent_done")
        ) + f"?company_id={company.pk}"

        try:
            result = ArcopayService.get_consent(
                bank_service=bank_service,
                callback_url=callback_url,
                callback_error_url=callback_error_url,
                redirect_url=redirect_url,
            )
            follow_url = result.get("follow", "")
            consent_id = result.get("consentId", "")
            token = result.get("token", "")

            if not follow_url:
                messages.error(request, _("Arcopay did not return a consent URL."))
                return redirect("admin:banking_connect_bank")

            # Store in session so ConsentDoneView can retrieve the token
            request.session["arcopay_consent_id"] = consent_id
            request.session["arcopay_company_id"] = str(company.pk)
            if token:
                request.session["arcopay_token"] = token

            logger.info(
                "Arcopay consent initiated: consentId=%s, token=%s, bank=%s, company=%s",
                consent_id, token or '(none)', bank_service, company.pk,
            )

            return redirect(follow_url)

        except Exception as e:
            logger.exception("Failed to initiate Arcopay consent")
            messages.error(
                request,
                _("Failed to connect to bank: %(error)s") % {"error": str(e)},
            )
            return redirect("admin:banking_connect_bank")

    def get(self, request):
        return redirect("admin:banking_connect_bank")


class ConsentDoneView(UnfoldModelAdminViewMixin, View):
    """Step 3: User returns here after completing bank authentication.

    Arcopay appends query params to the redirect URL:
        - statusAB: 200 (new consent) or 402 (consent already valid)
        - consentStatusAB: 'valid'
        - tokenAB: the Arcopay token for the consent

    For 200: the server-to-server callback should create the BankAccount.
    For 402: the callback won't fire again, so we fetch data directly.
    """

    title = _("Bank Connection Result")
    permission_required = ("banking.view_banktransaction",)

    def get(self, request):
        import time
        from company.models import BankAccount

        status = request.GET.get("statusAB", "")
        consent_status = request.GET.get("consentStatusAB", "")
        company_id = request.GET.get("company_id", "")

        # Retrieve session data stored during InitiateConsentView
        session_consent_id = request.session.pop("arcopay_consent_id", "")
        session_token = request.session.pop("arcopay_token", "")
        session_company_id = request.session.pop("arcopay_company_id", "") or company_id

        # 200 = new consent granted, 402 = consent already valid
        is_success = status in ("200", "402") or consent_status in ("valid", "Authorised")

        if not is_success:
            if status:
                messages.warning(
                    request,
                    _("Bank connection returned status: %(status)s (%(consent)s)")
                    % {"status": status, "consent": consent_status},
                )
            else:
                messages.info(request, _("Bank connection process completed."))
            return redirect("admin:banking_banktransaction_changelist")

        # Resolve the company
        company = None
        cid = session_company_id or company_id
        if cid:
            user_companies = get_user_companies(request.user)
            company = next((c for c in user_companies if str(c.pk) == str(cid)), None)

        # The callback (server-to-server) should create the BankAccount.
        # It may arrive before or after this redirect, so we poll briefly.
        token = session_token
        latest_account = None

        # Wait up to 15s for the callback to create/update a BankAccount
        for _attempt in range(15):
            latest_account = (
                BankAccount.objects
                .exclude(nuek_account_id="")
                .exclude(nuek_account_id__isnull=True)
                .order_by("-pk")
                .first()
            )
            if latest_account:
                break
            time.sleep(1)

        # If callback didn't create it (or 402), store directly using the token
        if not latest_account and token and company:
            try:
                latest_account = self._store_account_from_token(token, company)
            except Exception:
                logger.exception("Failed to store account from token")

        if latest_account:
            messages.success(
                request,
                _("Bank connected successfully! Your transactions are being imported."),
            )
            return redirect(
                reverse("admin:company_bankaccount_change", args=[latest_account.pk])
            )
        else:
            messages.success(
                request,
                _("Bank authorization completed. The account will appear shortly once processing finishes."),
            )
            return redirect("admin:banking_banktransaction_changelist")

    @staticmethod
    def _store_account_from_token(token: str, company) -> BankAccount | None:
        """Fetch account data from Arcopay using the token and store it."""
        from datetime import date, timedelta
        from company.models import BankAccount

        products = ArcopayService.fetch_transactions(
            token=token,
            start_date=date.today() - timedelta(days=90),
            products="GLOBAL",
        )

        if not products:
            return None

        account = None
        for product_data in products:
            account, created = BankAccount.upsert_from_arcopay(
                company=company,
                product_data=product_data,
                token=token,
            )
            ArcopayService.import_transactions_for_account(account, product_data)

        return account


class SyncAccountsView(UnfoldModelAdminViewMixin, View):
    """Manually trigger a sync of all connected bank accounts."""

    title = _("Sync Bank Accounts")
    permission_required = ("banking.view_banktransaction",)

    def post(self, request):
        company_id = request.POST.get("company_id", "")
        company = None
        if company_id:
            user_companies = get_user_companies(request.user)
            company = next((c for c in user_companies if str(c.pk) == str(company_id)), None)

        result = ArcopayService.sync_all_connected_accounts(company)
        return JsonResponse(result.to_dict())
