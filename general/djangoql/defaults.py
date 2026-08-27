"""DjangoQL field declarations for models this project doesn't own."""

from django.contrib.auth import get_user_model

from .registry import SearchFields, register


@register(get_user_model())
class UserSearchFields(SearchFields):
    fields = ("id", "username", "first_name", "last_name", "email", "is_active")
