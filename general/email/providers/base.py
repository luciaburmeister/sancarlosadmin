"""
Registry of the providers that carry our outgoing email and report back on it.

A provider is the adapter for one email service: it knows how to make that
service emit delivery and open events for a message we send, and how to
authenticate and read the webhooks it sends back.  Everything above it — the
`EmailLog` summary fields, the delivery-status vocabulary, the API filters —
is deliberately provider-neutral, so adding a second service is a new
declaration here and nothing else:

    # <app>/email_providers.py, or general/email/providers/<name>.py
    from general.email.providers import EmailProvider, register

    @register
    class MailgunProvider(EmailProvider):
        key = "mailgun"
        label = _("Mailgun")

The `key` is stored on every `EmailLog` and `EmailEvent` row and appears in the
webhook URL, so it must stay stable once messages have been sent with it.

Note what is *not* configurable here: each provider hardcodes its own trust
anchors — for Amazon, the hosts allowed to serve a signing certificate.  Which
provider runs is configuration; whether its webhook signatures are checked is
not, because these endpoints are public and unauthenticated.
"""

import pkgutil
from importlib import import_module

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import module_has_submodule

DECLARATIONS_MODULE = "email_providers"
DEFAULT_PROVIDER = "ses"

_REGISTRY = {}
_autodiscovered = False


class InvalidWebhook(ValueError):
    """The request did not authentically come from the provider."""


class EmailProvider:
    """
    One email service we can send through and receive events from.

    The outgoing half has a common shape across services: decide whether this
    kind of email is tracked, and tag the message so the service reports on it.
    The incoming half does not — SNS wraps the event in a signed envelope and
    fetches an X.509 certificate, Mailgun signs an HMAC over two form fields,
    Postmark signs nothing at all — so each provider owns its whole webhook
    flow rather than pretending to share one.
    """

    key = None
    label = None

    # --- outgoing -----------------------------------------------------------

    def tracking_enabled(self, policy):
        """
        Is this provider configured to emit events for this tracking policy?

        `policy` is the `{"delivery": bool, "opens": bool}` declared by the
        `EmailDocument`.  False means the send falls back to plain SMTP.
        """
        return False

    def outgoing_headers(self, *, log, batch_id, policy):
        """Transport headers that make the provider report on this message."""
        return {}

    # --- incoming -----------------------------------------------------------

    def handle_webhook(self, request):
        """Authenticate and apply one webhook. Returns `(payload, status)`."""
        raise NotImplementedError


def register(declaration):
    """Class decorator registering an `EmailProvider` subclass."""
    if not (isinstance(declaration, type) and issubclass(declaration, EmailProvider)):
        raise ImproperlyConfigured(
            "@register must decorate an EmailProvider subclass, got %r" % (declaration,)
        )
    if not declaration.key:
        raise ImproperlyConfigured("%s declares no key" % declaration.__name__)
    if not declaration.label:
        raise ImproperlyConfigured("%s declares no label" % declaration.__name__)

    registered = _REGISTRY.get(declaration.key)
    if registered is not None and type(registered) is not declaration:
        raise ImproperlyConfigured(
            "Email provider '%s' is already registered by %s"
            % (declaration.key, type(registered).__name__)
        )
    # Providers are stateless adapters: one instance each is enough, and it
    # reads settings lazily so importing this module never touches them.
    _REGISTRY[declaration.key] = declaration()
    return declaration


def autodiscover():
    """Import the built-in providers, then any an app declares, once."""
    global _autodiscovered
    if _autodiscovered:
        return
    _autodiscovered = True

    package = import_module(__package__)
    for _, name, _ in pkgutil.iter_modules(package.__path__):
        if name != "base":
            import_module("%s.%s" % (__package__, name))

    for app_config in apps.get_app_configs():
        if module_has_submodule(app_config.module, DECLARATIONS_MODULE):
            import_module("%s.%s" % (app_config.name, DECLARATIONS_MODULE))


def registered_providers():
    """Every registered provider, ordered by key."""
    autodiscover()
    return tuple(sorted(_REGISTRY.values(), key=lambda provider: provider.key))


def get_provider(key):
    """The provider registered under `key`, or None when there is none."""
    autodiscover()
    return _REGISTRY.get(key)


def default_provider():
    """
    The provider new sends go through.

    Per-company provider configuration would replace this lookup; until then
    one setting picks it for the whole installation.
    """
    return get_provider(getattr(settings, "EMAIL_TRACKING_PROVIDER", DEFAULT_PROVIDER))
