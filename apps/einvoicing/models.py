from django.db import models
from django.utils.translation import gettext_lazy as _

from company.models import Company
from apps.einvoicing.encryption import encrypt_value, decrypt_value


class EInvoicingProviderChoices(models.TextChoices):
    VERIFACTI = "verifacti", _("Verifacti")


class EInvoicingAccount(models.Model):
    """
    Stores provider credentials for e-invoicing (Verifactu / AEAT).
    One company can have multiple accounts (multi-NIF support).
    """

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="einvoicing_accounts",
        verbose_name=_("Company"),
    )
    provider = models.CharField(
        max_length=30,
        choices=EInvoicingProviderChoices.choices,
        default=EInvoicingProviderChoices.VERIFACTI,
        verbose_name=_("Provider"),
    )
    name = models.CharField(
        max_length=150,
        verbose_name=_("Account Name"),
        help_text=_("Human-readable label, e.g. 'Fistex S.L. – Production'"),
    )
    api_key_encrypted = models.TextField(
        blank=True,
        verbose_name=_("API Key (encrypted)"),
        help_text=_("Stored encrypted. Use the 'API Key' field to set a new value."),
    )
    invoice_serie = models.CharField(
        max_length=30,
        default="INV",
        verbose_name=_("Invoice Serie"),
        help_text=_("Serie identifier sent with every invoice (e.g. 'INV', 'F')."),
    )
    base_url = models.URLField(
        default="https://api.verifacti.com",
        verbose_name=_("API Base URL"),
        help_text=_("Override for sandbox/testing environments."),
    )
    is_sandbox = models.BooleanField(
        default=True,
        verbose_name=_("Sandbox Mode"),
        help_text=_("Enable for testing. Disable when going live."),
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name=_("Active"),
    )

    class Meta:
        verbose_name = _("E-Invoicing Account")
        verbose_name_plural = _("E-Invoicing Accounts")
        ordering = ["company", "name"]

    def __str__(self):
        return f"{self.name} ({self.get_provider_display()})"

    # ── API key helpers ──────────────────────────────────────────────────
    def set_api_key(self, plaintext: str) -> None:
        """Encrypt and store the API key."""
        self.api_key_encrypted = encrypt_value(plaintext)

    def get_api_key(self) -> str:
        """Decrypt and return the stored API key."""
        if not self.api_key_encrypted:
            return ""
        return decrypt_value(self.api_key_encrypted)


class SubmissionAction(models.TextChoices):
    CREATE = "create", _("Create")
    CANCEL = "cancel", _("Cancel")


class SubmissionStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    SUBMITTED = "submitted", _("Submitted")
    ACCEPTED = "accepted", _("Accepted")
    REJECTED = "rejected", _("Rejected")
    ERROR = "error", _("Error")


class EInvoicingSubmission(models.Model):
    """
    Audit log for every submission attempt to an e-invoicing provider.
    """

    account = models.ForeignKey(
        EInvoicingAccount,
        on_delete=models.PROTECT,
        related_name="submissions",
        verbose_name=_("Account"),
    )
    sales_invoice = models.ForeignKey(
        "invoices.SalesInvoice",
        on_delete=models.CASCADE,
        related_name="einvoicing_submissions",
        verbose_name=_("Sales Invoice"),
    )
    action = models.CharField(
        max_length=10,
        choices=SubmissionAction.choices,
        verbose_name=_("Action"),
    )
    status = models.CharField(
        max_length=15,
        choices=SubmissionStatus.choices,
        default=SubmissionStatus.PENDING,
        verbose_name=_("Status"),
    )
    request_payload = models.JSONField(
        blank=True,
        null=True,
        verbose_name=_("Request Payload"),
    )
    response_payload = models.JSONField(
        blank=True,
        null=True,
        verbose_name=_("Response Payload"),
    )
    provider_record_id = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("Provider Record ID"),
        help_text=_("Identifier returned by the provider for this record."),
    )
    error_message = models.TextField(
        blank=True,
        verbose_name=_("Error Message"),
    )
    submitted_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Submitted At"),
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_("Created At"),
    )

    class Meta:
        verbose_name = _("E-Invoicing Submission")
        verbose_name_plural = _("E-Invoicing Submissions")
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"{self.get_action_display()} – {self.sales_invoice} – "
            f"{self.get_status_display()}"
        )
