"""Fixture builders for the public API tests."""
from django.contrib.auth.models import Permission, User

from apps.public_api.models import UserAPIKey
from company.models import CompanyUser


def make_api_user(*, company, username="integration", perms=()):
    """An active user with access to ``company`` holding ``perms`` ("app.codename")."""
    user = User.objects.create_user(username)
    CompanyUser.objects.create(user=user, company=company)
    for perm in perms:
        app_label, codename = perm.split(".")
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename)
        )
    return user


def make_api_key(*, user, company, name="Integration", **fields):
    """A key of ``user`` on ``company``: (key, secret to send)."""
    return UserAPIKey.objects.create_key(name=name, user=user, company=company, **fields)


def auth_header(secret: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Api-Key {secret}"}
