from apps.expenses.models import ExpenseClaim, ExpenseClaimStatus
from payments.models import PaymentDirection
from payments.views import RegisterPaymentView


class ExpenseClaimRegisterPaymentView(RegisterPaymentView):
    """Reimbursing the employee who submitted the claim, so the money goes out."""

    model = ExpenseClaim
    direction = PaymentDirection.OUTBOUND
    change_url_name = "admin:expenses_expenseclaim_change"

    def get_document_total(self, document):
        return document.total_claimed

    def get_max_amount(self, document):
        # recalculate_reimbursement() clamps total_reimbursed at total_claimed,
        # so an over-payment would silently disagree with the allocation it came
        # from. Stop it at the form instead.
        return document.amount_remaining.amount

    def can_register_payment(self, request, document):
        # Mirrors ExpenseClaimAdmin.has_register_payment_detail_action_permission,
        # which decides whether the button is shown. Without this the URL would
        # still be reachable by any staff user who typed it.
        if document.status != ExpenseClaimStatus.APPROVED:
            return False
        if document.amount_remaining.amount <= 0:
            return False
        return request.user.is_superuser or request.user.has_perm("expenses.approve_expenseclaim")
