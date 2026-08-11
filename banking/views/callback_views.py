"""Arcopay webhook callback handlers (public, CSRF-exempt).

These views receive server-to-server POST requests from Arcopay's
infrastructure after the PSD2 consent flow completes.
"""
import json
import logging

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from banking.services.arcopay_import import ArcopayService
from company.models import BankAccount, Company

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class ArcopayCallbackView(View):
    """POST /api/banking/arcopay/callback/

    Called by Arcopay after a user successfully completes PSD2 consent.

    Expected payload (consentGetCallback):
        {
            "token": "...",
            "consentId": "...",
            "globalPosition": [
                {
                    "product": "ES1234567890123456789012",
                    "type": "checking",
                    "balance": "1234.56",
                    "currency": "EUR",
                    "transactions": [...]
                }
            ]
        }

    Must respond with {"result": "OK"} for Arcopay to consider it successful.
    """

    def post(self, request):
        logger.info(
            "Arcopay callback received: content_type=%s, query=%s, body_len=%d",
            request.content_type, request.GET.dict(), len(request.body),
        )
        logger.debug("Arcopay callback raw body: %s", request.body[:2000])

        try:
            if request.content_type and "json" in request.content_type:
                payload = json.loads(request.body)
            else:
                # Arcopay may send as form-encoded
                payload = {
                    "token": request.POST.get("token", ""),
                    "consentId": request.POST.get("consentId", ""),
                }
                gp_raw = request.POST.get("globalPosition", "")
                if gp_raw:
                    try:
                        payload["globalPosition"] = json.loads(gp_raw)
                    except json.JSONDecodeError:
                        payload["globalPosition"] = []
                else:
                    payload["globalPosition"] = []
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("Arcopay callback: invalid payload — %s", e)
            return JsonResponse({"result": "KO", "error": "Invalid payload"}, status=400)

        logger.info("Arcopay callback parsed payload: token=%s, consentId=%s, products=%d",
                     payload.get("token", "")[:20],
                     payload.get("consentId", ""),
                     len(payload.get("globalPosition", [])))

        company_id = request.GET.get("company_id")
        if not company_id:
            logger.error("Arcopay callback: no company_id in query string")
            return JsonResponse({"result": "KO", "error": "Missing company_id"}, status=400)

        company = Company.objects.filter(pk=company_id, is_active=True).first()
        if not company:
            logger.error("Arcopay callback: no active company found for id=%s", company_id)
            return JsonResponse({"result": "KO", "error": "No company"}, status=400)

        # Optional: the account the reconnect flow was started from. The
        # Arcopay response doesn't identify the account, so this is the only
        # reliable way to bind the new token/consent to a specific account.
        bank_account = None
        bank_account_id = request.GET.get("bank_account_id")
        if bank_account_id:
            bank_account = BankAccount.objects.filter(
                pk=bank_account_id, company=company,
            ).first()
            if not bank_account:
                logger.warning(
                    "Arcopay callback: bank_account_id=%s not found for company %s; "
                    "continuing without account binding.", bank_account_id, company.pk,
                )

        try:
            result = ArcopayService.process_consent_callback(
                payload, company, bank_account=bank_account,
            )
            logger.info(
                "Arcopay consent callback processed: consent_id=%s, accounts=%d",
                result.get("consent_id"),
                len(result.get("accounts", [])),
            )
            return JsonResponse({"result": "OK"})
        except Exception as e:
            logger.exception("Arcopay callback processing error")
            return JsonResponse({"result": "KO", "error": str(e)}, status=500)


@method_decorator(csrf_exempt, name="dispatch")
class ArcopayErrorCallbackView(View):
    """POST /api/banking/arcopay/callback-error/

    Called by Arcopay when consent flow encounters an error.

    Expected payload (consentGetCallbackError):
        {
            "message": "...",
            "code": "...",
            "consentId": "..."
        }
    """

    def post(self, request):
        try:
            if request.content_type and "json" in request.content_type:
                payload = json.loads(request.body)
            else:
                payload = {
                    "message": request.POST.get("message", ""),
                    "code": request.POST.get("code", ""),
                    "consentId": request.POST.get("consentId", ""),
                }
        except (json.JSONDecodeError, ValueError):
            payload = {}

        logger.warning(
            "Arcopay consent error callback: code=%s, message=%s, consentId=%s",
            payload.get("code", "unknown"),
            payload.get("message", "unknown"),
            payload.get("consentId", "unknown"),
        )

        return JsonResponse({"result": "OK"})
