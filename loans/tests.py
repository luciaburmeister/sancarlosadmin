from unittest.mock import patch

from django.test import SimpleTestCase
from djmoney.money import Money

from loans.models import Loan, LoanInstallment


class LoanModelTests(SimpleTestCase):
    def test_queue_bank_account_sync_does_not_raise_when_task_fails(self):
        loan = Loan(bank_account_id=1)

        with patch("banking.tasks.sync_bank_account_task", side_effect=RuntimeError("boom")) as mock_task:
            loan._queue_bank_account_sync()

        mock_task.assert_called_once_with(1)


