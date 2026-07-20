from core.services.file_duplicate_service import FileDuplicateService
from apps.expenses.models import ExpenseTicket


class ExpenseTicketHashError(Exception):
    pass


class ExpenseTicketDuplicateService(FileDuplicateService):
    model = ExpenseTicket
    hash_field = "file_hash"
    hash_error_class = ExpenseTicketHashError

    def check_for_duplicate_file(self, uploaded_file, company_id: int):
        return super().check_for_duplicate_file(uploaded_file, company_id=company_id)
