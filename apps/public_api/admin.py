from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from rest_framework_api_key.admin import APIKeyModelAdmin
from unfold.admin import ModelAdmin, TabularInline

from apps.public_api.models import UserAPIKey


@admin.register(UserAPIKey)
class UserAPIKeyAdmin(APIKeyModelAdmin, ModelAdmin):
    """
    The secret is shown once, in a message, when the key is saved for the first
    time (APIKeyModelAdmin.save_model); only its hash is stored. Revoking can't
    be undone: create a new key instead.
    """

    list_display = ("name", "user", "company", "prefix", "created", "last_used_at", "expiry_date", "_has_expired", "revoked")
    list_filter = ("company", "revoked", "created")
    search_fields = ("name", "prefix", "user__username", "user__email", "company__name")
    autocomplete_fields = ("user", "company")
    fields = ("name", "user", "company", "expiry_date", "revoked", "prefix", "created", "last_used_at")

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj) + ("created", "last_used_at")
        if obj is not None:
            # A key's owner never changes: make a new key for someone else
            fields += ("user", "company")
        return fields


class UserAPIKeyInline(TabularInline):
    """Read-only list of a user's keys on the user page; keys are managed in their own admin."""

    model = UserAPIKey
    fk_name = "user"
    extra = 0
    can_delete = False
    show_change_link = True
    fields = ("name", "company", "prefix", "created", "last_used_at", "expiry_date", "revoked")
    readonly_fields = fields
    verbose_name = _("API key")
    verbose_name_plural = _("API keys")

    def has_add_permission(self, request, obj=None):
        return False
