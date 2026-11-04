from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _
from rest_framework_api_key.models import AbstractAPIKey

from company.active_company import ActiveCompany


def user_can_access_company(user, company_id) -> bool:
    """Same rule as the admin: active membership of an active company, or superuser."""
    return ActiveCompany.allowed_for(user).filter(pk=company_id).exists()


class UserAPIKey(AbstractAPIKey):
    """
    An API key for the public API. A request made with it acts as ``user`` on
    ``company``: it can do exactly what the user's permissions allow, on that
    company's data only. The secret is shown once, when the key is created;
    only its hash is stored. Revoking is final.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="api_keys",
        verbose_name=_("User"),
    )
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="api_keys",
        verbose_name=_("Company"),
    )
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False, verbose_name=_("Last used"))

    class Meta(AbstractAPIKey.Meta):
        verbose_name = _("API key")
        verbose_name_plural = _("API keys")

    def clean(self):
        super().clean()
        if self.user_id and self.company_id and not user_can_access_company(self.user, self.company_id):
            raise ValidationError({"company": _("The user has no access to this company.")})


class IdempotencyRecord(models.Model):
    """
    The response to a POST sent with an ``Idempotency-Key`` header, so a
    client retrying the same request gets the same answer instead of creating
    the resource twice. ``response_status`` is null while the first request is
    still running. Kept 24 hours.
    """

    api_key = models.ForeignKey(UserAPIKey, on_delete=models.CASCADE, related_name="idempotency_records")
    key = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["api_key", "key"], name="public_api_idempotency_key_unique"),
        ]

    def __str__(self):
        return self.key
