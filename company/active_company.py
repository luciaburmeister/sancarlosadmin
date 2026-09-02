# company/active_company.py

import time
from collections.abc import Iterable

from asgiref.local import Local
from django.core.cache import cache
from django.db.models import QuerySet

from company.models import Company, CompanyUser


class ActiveCompany:
    """
    Owns everything about the per-user active companies:

    - which companies a user is ALLOWED to see (cached, 5 min)
    - which companies are ACTIVE right now (session, ~1 day). One or more:
      the switcher toggles companies in and out of the set, so a user can
      work on several at the same time.
    - a request-scoped holder so code without ``request`` access
      (``get_user_companies``) can read the active ids.

    The session is the persistent store (DB-backed, shared across all
    workers); the ``Local`` is per-request transport, repopulated by
    ``ActiveCompanyMiddleware`` on every request. Per-thread/coroutine on
    purpose, to isolate concurrent requests (same pattern as
    simple_history's middleware). Outside a request (celery, shell,
    management commands) the holder is unset and callers see the full
    allowed list, exactly as before this feature existed.
    """

    SESSION_KEY = "active_company_ids"
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
    def set_current_ids(cls, company_ids: Iterable[int] | None) -> None:
        """``None`` means "no narrowing" (outside a request / nothing active)."""
        cls._state.company_ids = None if company_ids is None else list(company_ids)

    @classmethod
    def clear_current(cls) -> None:
        cls._state.company_ids = None

    @classmethod
    def get_current_ids(cls) -> list[int] | None:
        return getattr(cls._state, "company_ids", None)

    # ---- session persistence -----------------------------------------

    @classmethod
    def activate(cls, request, companies: Iterable[Company]) -> list[Company]:
        """
        Persist the active set: session keys + fresh timestamp + holder.
        Returns the stored companies, sorted by name.
        """
        companies = sorted(companies, key=lambda c: (c.name, c.pk))
        for company in companies:
            cache.set(
                cls.COMPANY_OBJ_CACHE_KEY.format(company_pk=company.pk),
                company,
                cls.CACHE_TIMEOUT,
            )
        cls._store_ids(request, [c.pk for c in companies])
        return companies

    @classmethod
    def toggle(cls, request, company: Company) -> bool | None:
        """
        Add ``company`` to the active set if absent, remove it if present.
        Returns True when added, False when removed, ``None`` when the
        removal was refused because it would leave the set empty (at least
        one company must stay active).
        """
        current = list(cls.get_current_ids() or [])

        if company.pk in current:
            if len(current) == 1:
                return None
            current.remove(company.pk)
            cls._store_ids(request, current)
            return False

        cls.activate(request, [*cls.get_companies(current), company])
        return True

    @classmethod
    def _store_ids(cls, request, company_ids: list[int]) -> None:
        request.session[cls.SESSION_KEY] = company_ids
        request.session[cls.SESSION_SET_AT_KEY] = time.time()
        cls.set_current_ids(company_ids or None)

    @classmethod
    def resolve(cls, request) -> list[Company]:
        """
        Called by the middleware on every request. Returns the active
        companies for ``request.user`` (sorted by name), defaulting
        silently to the first switchable company when the stored choice is
        missing, invalid or older than ``MAX_AGE``. Ids the user has lost
        access to are dropped silently; the rest survive.
        Steady-state cost: session read + cache gets, no DB queries.
        """
        switchable_ids = cls.switchable_ids_for(request.user)

        raw = request.session.get(cls.SESSION_KEY)
        set_at = request.session.get(cls.SESSION_SET_AT_KEY)

        # Sessions written before the multi-company switcher hold a bare int.
        stored = raw if isinstance(raw, list) else ([raw] if raw is not None else [])
        valid_ids = [cid for cid in stored if cid in switchable_ids]

        if (
            valid_ids
            and set_at is not None
            and time.time() - set_at < cls.MAX_AGE
        ):
            companies = cls.get_companies(valid_ids)
            if isinstance(raw, list) and len(companies) == len(stored):
                cls.set_current_ids([c.pk for c in companies])
                return companies
            # Legacy int value, or some ids revoked/deleted: persist the
            # cleaned-up list.
            if companies:
                return cls.activate(request, companies)

        # Missing / invalid / expired -> deterministic silent default.
        default = cls.switchable_for(request.user).order_by("name").first()
        if default is not None:
            return cls.activate(request, [default])

        request.session.pop(cls.SESSION_KEY, None)
        request.session.pop(cls.SESSION_SET_AT_KEY, None)
        cls.set_current_ids(None)
        return []

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

    @classmethod
    def get_companies(cls, company_ids: Iterable[int]) -> list[Company]:
        """Cached instances for ``company_ids`` (unknown ids skipped), sorted by name."""
        companies = (cls.get_company(cid) for cid in company_ids)
        return sorted(
            (c for c in companies if c is not None), key=lambda c: (c.name, c.pk)
        )
