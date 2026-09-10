"""
Registry of the kinds of email the system sends out.

A kind of email is configurable - a company can point its Reply-To at a
different mailbox for it - only once it is registered here, the same way a
model shows up in the admin only once a ModelAdmin is registered:

    # <app>/email_documents.py, or a module inside <app>/email_documents/
    from general.email import EmailDocument, register

    @register
    class SalesInvoiceEmail(EmailDocument):
        key = "sales_invoice"
        label = _("Sales Invoice")

The `key` is what call sites pass to `EmailService.send_email(email_document=...)`
and what is stored on `company.EmailReplyTo`, so it must stay stable once
companies have configured it. The `label` is what admins pick from.

Declarations are imported automatically (see `autodiscover`).
"""

import pkgutil
from importlib import import_module

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import module_has_submodule

DECLARATIONS_MODULE = "email_documents"

_REGISTRY = {}
_autodiscovered = False


class EmailDocument:
    """
    One kind of outgoing email.

    `key` is the stable identifier stored in the database; `label` is the
    human name shown when configuring a Reply-To for it.
    """

    key = None
    label = None


def register(declaration):
    """Class decorator registering an `EmailDocument` subclass."""
    if not (isinstance(declaration, type) and issubclass(declaration, EmailDocument)):
        raise ImproperlyConfigured(
            "@register must decorate an EmailDocument subclass, got %r" % (declaration,)
        )
    key = declaration.key
    if not key:
        raise ImproperlyConfigured("%s declares no key" % declaration.__name__)
    if not declaration.label:
        raise ImproperlyConfigured("%s declares no label" % declaration.__name__)

    registered = _REGISTRY.get(key)
    if registered is not None and registered is not declaration:
        raise ImproperlyConfigured(
            "Email document '%s' is already registered by %s"
            % (key, registered.__name__)
        )
    _REGISTRY[key] = declaration
    return declaration


def autodiscover():
    """
    Import every app's email document declarations, once.

    An app can use either `email_documents.py` or an `email_documents/`
    package, in which case every module inside it is imported too.
    """
    global _autodiscovered
    if _autodiscovered:
        return
    _autodiscovered = True

    for app_config in apps.get_app_configs():
        if not module_has_submodule(app_config.module, DECLARATIONS_MODULE):
            continue
        module_name = "%s.%s" % (app_config.name, DECLARATIONS_MODULE)
        module = import_module(module_name)
        for _, name, _ in pkgutil.iter_modules(getattr(module, "__path__", [])):
            import_module("%s.%s" % (module_name, name))


def registered_documents():
    """All registered declarations, ordered by label."""
    autodiscover()
    return tuple(sorted(_REGISTRY.values(), key=lambda d: str(d.label)))


def document_choices():
    """`(key, label)` pairs for form fields. Not frozen into migrations."""
    return [(d.key, d.label) for d in registered_documents()]


def document_label(key):
    """The label registered for `key`, or `key` itself if unknown."""
    autodiscover()
    declaration = _REGISTRY.get(key)
    return declaration.label if declaration else key


def is_registered(key):
    autodiscover()
    return key in _REGISTRY
