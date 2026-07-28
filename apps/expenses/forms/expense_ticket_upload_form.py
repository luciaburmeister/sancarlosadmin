from django import forms
from django.core.exceptions import ValidationError
from unfold.widgets import UnfoldAdminSelectWidget
from apps.expenses.models import ExpenseTicket
from apps.expenses.services.expense_ticket_duplicate_service import ExpenseTicketDuplicateService, ExpenseTicketHashError
from core.admin.company_access import get_user_companies


class ExpenseTicketUploadForm(forms.ModelForm):
    class Meta:
        model = ExpenseTicket
        fields = ("company", "photo")
        widgets = {"company": UnfoldAdminSelectWidget}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if user is not None:
            self.fields["company"].queryset = get_user_companies(user)

    def clean(self):
        cleaned_data = super().clean()
        f = cleaned_data.get("photo")
        company = cleaned_data.get("company")

        if not f or not company:
            return cleaned_data

        duplicate_service = ExpenseTicketDuplicateService()
        try:
            result = duplicate_service.check_for_duplicate_file(f, company.id)
        except ExpenseTicketHashError as e:
            raise ValidationError(f"Could not read file for duplicate check: {e}")

        if result.is_duplicate:
            existing = result.similar_object
            raise ValidationError(
                f"This receipt has already been uploaded (Ticket #{existing.pk} – {existing.vendor_name})."
            )

        self._computed_file_hash = result.hash_value
        return cleaned_data

    def save(self, commit=True):
        obj = super().save(commit=False)
        if hasattr(self, "_computed_file_hash"):
            obj.file_hash = self._computed_file_hash
        if commit:
            obj.save()
            self.save_m2m()
        return obj
