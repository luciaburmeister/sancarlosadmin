# company/active_company.py

import time

from asgiref.local import Local
from django.core.cache import cache
from django.db.models import QuerySet

from company.models import Company, CompanyUser


class ActiveCompany:
    """
    Owns everything about the per-user active company:

    - which companies a user is ALLOWED to see (cached, 5 min)
    - which single company is ACTIVE right now (session, ~1 day)
    - a request-scoped holder so code without ``request`` access
      (``get_user_companies``) can read the active id.

    The session is the persistent store (DB-backed, shared across all
    workers); the ``Local`` is per-request transport, repopulated by
    ``ActiveCompanyMiddleware`` on every request. Per-thread/coroutine on
    purpose, to isolate concurrent requests (same pattern as
    simple_history's middleware). Outside a request (celery, shell,
    management commands) the holder is unset and callers see the full
    allowed list, exactly as before this feature existed.
    """

    SESSION_KEY = "active_company_id"
    SESSION_SET_AT_KEY = "active_company_set_at"
    MAX_AGE = 60 * 60 * 24  # ~1 day

    CACHE_TIMEOUT = 60 * 5  # 5 minutes
    MEMBERSHIP_CACHE_KEY = "admin:user_companies:{user_pk}"
    SUPERUSER_IDS_CACHE_KEY = "admin:switchable_company_ids:super"
    COMPANY_OBJ_CACHE_KEY = "active_company:obj:{company_pk}"

    _state = Local()

    # ---- allowed set -------------------------------------------------

    @classmethod
    def allowed_for(cls, user) -> QuerySet[Company]:
        """
        Companies the user is allowed to access at all.
        Superusers get all companies. Cached per-user (5 min).
        """
        if not user.is_authenticated:
            return Company.objects.none()

        if user.is_superuser:
            return Company.objects.all()

        return Company.objects.filter(id__in=cls._membership_ids(user))

    @classmethod
    def switchable_for(cls, user) -> QuerySet[Company]:
        """
        Companies the user can pick in the switcher: superusers may pick
        any active company; everyone else their allowed set (which is
        already restricted to active companies).
        """
        if not user.is_authenticated:
            return Company.objects.none()

        if user.is_superuser:
            return Company.objects.filter(is_active=True)

        return cls.allowed_for(user)

    @classmethod
    def switchable_ids_for(cls, user) -> list[int]:
        """Cached id list used to validate the session value without a query."""
        if not user.is_authenticated:
            return []

        if user.is_superuser:
            ids = cache.get(cls.SUPERUSER_IDS_CACHE_KEY)
            if ids is None:
                ids = list(
                    Company.objects.filter(is_active=True).values_list("id", flat=True)
                )
                cache.set(cls.SUPERUSER_IDS_CACHE_KEY, ids, cls.CACHE_TIMEOUT)
            return ids

        return cls._membership_ids(user)

    @classmethod
    def _membership_ids(cls, user) -> list[int]:
        cache_key = cls.MEMBERSHIP_CACHE_KEY.format(user_pk=user.pk)
        company_ids = cache.get(cache_key)

        if company_ids is None:
            company_ids = list(
                CompanyUser.objects.filter(
                    user=user,
                    is_active=True,
                    company__is_active=True,
                ).values_list("company_id", flat=True)
            )
            cache.set(cache_key, company_ids, cls.CACHE_TIMEOUT)

        return company_ids

    # ---- request-scoped holder ---------------------------------------

    @classmethod
    def set_current_id(cls, company_id: int | None) -> None:
        cls._state.company_id = company_id

    @classmethod
    def clear_current(cls) -> None:
        cls._state.company_id = None

    @classmethod
    def get_current_id(cls) -> int | None:
        return getattr(cls._state, "company_id", None)

    # ---- session persistence -----------------------------------------

    @classmethod
    def activate(cls, request, company: Company) -> None:
        """Persist the choice: session keys + fresh timestamp + holder."""
        request.session[cls.SESSION_KEY] = company.pk
        request.session[cls.SESSION_SET_AT_KEY] = time.time()
        cache.set(
            cls.COMPANY_OBJ_CACHE_KEY.format(company_pk=company.pk),
            company,
            cls.CACHE_TIMEOUT,
        )
        cls.set_current_id(company.pk)

    @classmethod
    def resolve(cls, request) -> Company | None:
        """
        Called by the middleware on every request. Returns the active
        company for ``request.user``, defaulting silently when the stored
        choice is missing, invalid or older than ``MAX_AGE``.
        Steady-state cost: session read + cache gets, no DB queries.
        """
        switchable_ids = cls.switchable_ids_for(request.user)

        company_id = request.session.get(cls.SESSION_KEY)
        set_at = request.session.get(cls.SESSION_SET_AT_KEY)

        if (
            company_id in switchable_ids
            and set_at is not None
            and time.time() - set_at < cls.MAX_AGE
        ):
            return cls.get_company(company_id)

        # Missing / invalid / expired -> deterministic silent default.
        default = cls.switchable_for(request.user).order_by("name").first()
        if default is not None:
            cls.activate(request, default)
        else:
            request.session.pop(cls.SESSION_KEY, None)
            request.session.pop(cls.SESSION_SET_AT_KEY, None)
        return default

    @classmethod
    def get_company(cls, company_id: int) -> Company | None:
        """Company instance by id, cached (5 min) to keep resolve() query-free."""
        cache_key = cls.COMPANY_OBJ_CACHE_KEY.format(company_pk=company_id)
        company = cache.get(cache_key)

        if company is None:
            company = Company.objects.filter(pk=company_id).first()
            if company is not None:
                cache.set(cache_key, company, cls.CACHE_TIMEOUT)

        return company
