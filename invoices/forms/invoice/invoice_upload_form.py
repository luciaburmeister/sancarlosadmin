from django import forms
from django.core.exceptions import ValidationError
from invoices.models import Invoice
from unfold.widgets import UnfoldAdminFileFieldWidget
from invoices.services.invoice.invoice_duplicate_service import InvoiceDuplicateService, InvoiceHashError

from django.utils.safestring import mark_safe
from django.urls import reverse


class InvoiceUploadForm(forms.ModelForm):
    class Meta:
        model = Invoice
        fields = ("file",)
        widgets = {
            "file": UnfoldAdminFileFieldWidget(attrs={"accept": ".pdf,.jpeg,.jpg"}),
        }
        class Media:
            js = [
            f"admin/js/vendor/jquery/jquery.js",
            "admin/js/jquery.init.js",
            "admin/js/calendar.js",
            "admin/js/admin/DateTimeShortcuts.js",
            "admin/js/core.js",
        ]
            
    def clean(self):
        cleaned_data = super().clean()
        f = cleaned_data.get("file")

        if not f:
            return cleaned_data

        duplicate_service = InvoiceDuplicateService(similarity_threshold=5)
        try:
            result = duplicate_service.check_for_duplicate_file(f)
        except InvoiceHashError as e:
            raise ValidationError(f"Could not read PDF for duplicate check: {e}")
        
        if result.is_duplicate:
            existing = result.similar_invoice
            url = reverse("admin:invoices_invoice_change", args=[existing.pk])

            raise ValidationError(
                mark_safe(
                    f'This invoice is a duplicate of '
                    f'<a href="{url}" target="_blank">'
                    f'Invoice #{existing.pk} ({existing.invoice_number})'
                    f'</a>. '
                    f'Similarity score: {result.similarity_distance}.'
                )
            )

        self._computed_image_hash = result.hash_value
        return cleaned_data

    def save(self, commit=True):
        obj = super().save(commit=False)
        if hasattr(self, "_computed_image_hash"):
            obj.image_hash = self._computed_image_hash
        if commit:
            obj.save()
            self.save_m2m()
        return obj