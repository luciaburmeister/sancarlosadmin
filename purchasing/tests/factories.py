"""
purchasing/tests/factories.py

Fixture builders shared by the purchasing test modules. Each one is a thin
keyword-only wrapper around ``objects.create`` with the defaults the tests
used to spell out by hand.
"""
import time
from datetime import date

from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.cache import cache
from django.test import RequestFactory

from company.active_company import ActiveCompany
from company.models import Company
from materials.models import Provider
from purchasing.models import PurchaseOrder


def make_company(*, name="Test Company", tax_identifier="B12345678", **fields):
    """Create a company; ``tax_identifier`` must differ between two companies."""
    return Company.objects.create(name=name, tax_identifier=tax_identifier, **fields)


def make_provider(*, company, name="Proveedor SA", **fields):
    """Create a provider of ``company``."""
    return Provider.objects.create(company=company, name=name, **fields)


def make_purchase_order(*, company, provider, date_ordered=date(2026, 8, 1), save=True, **fields):
    """Create a purchase order; ``save=False`` returns the unsaved instance."""
    po = PurchaseOrder(company=company, provider=provider, date_ordered=date_ordered, **fields)
    if save:
        po.save()
    return po


def make_admin_request(*, user, data=None):
    """A POST request carrying a session and messages, as the admin actions expect."""
    request = RequestFactory().post("/", data or {})
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    MessageMiddleware(lambda r: None).process_request(request)
    request.user = user
    return request


def login_scoped(*, client, user, company):
    """Log ``user`` in on ``client`` with ``company`` as the only active company."""
    client.force_login(user)
    cache.delete(ActiveCompany.SUPERUSER_IDS_CACHE_KEY)
    session = client.session
    session[ActiveCompany.SESSION_KEY] = [company.pk]
    session[ActiveCompany.SESSION_SET_AT_KEY] = time.time()
    session.save()
