"""Banking tools — cached account balances.

Bespoke: BankingDataset (apps.reports) covers transaction rows, not the
account-level cached ``last_known_balance`` this reads.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from apps.ai_agent.interfaces import BaseTool, ChatContext, ToolExecutionError
from apps.ai_agent.registry import register_tool
from core.admin.company_access import get_user_companies

User = get_user_model()


@register_tool
class GetBankBalances(BaseTool):
    @property
    def name(self) -> str:
        return "get_bank_balances"

    @property
    def description(self) -> str:
        return (
            "Get the latest known balance of the company's linked bank "
            "accounts. Returns bank name, IBAN, balance, currency, and "
            "when it was last updated."
        )

    @property
    def required_permission(self) -> str | None:
        return "banking.view_banktransaction"

    def get_parameters_schema(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, context: ChatContext, params: dict) -> dict:
        from company.models import BankAccount

        try:
            user = User.objects.get(pk=context.user_id)
        except User.DoesNotExist as exc:
            raise ToolExecutionError("User not found.") from exc

        accounts = BankAccount.objects.filter(
            company__in=get_user_companies(user),
            last_known_balance__isnull=False,
        ).order_by("bank_name")

        return {
            "accounts": [
                {
                    "bank_name": a.bank_name,
                    "iban": a.iban or a.nuek_product_id or "",
                    "balance": (
                        str(a.last_known_balance.amount) if a.last_known_balance else "0"
                    ),
                    "currency": a.last_known_balance_currency or "EUR",
                    "last_updated": (
                        a.last_balance_updated_at.strftime("%d/%m/%Y %H:%M")
                        if a.last_balance_updated_at else ""
                    ),
                }
                for a in accounts
            ],
            "total_count": accounts.count(),
        }
