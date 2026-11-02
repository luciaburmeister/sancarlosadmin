"""
Registry of the steps a new company goes through before it can operate.

A step is shown in the setup of a company only once it is registered here, the
same way a model shows up in the admin only once a ModelAdmin is registered:

    # <app>/onboarding.py
    from general.onboarding import OnboardingStep, register

    @register
    class WarehousesStep(OnboardingStep):
        key = "warehouses"
        title = _("Warehouses")
        order = 60
        module = CompanyModule.INVENTORY

        def sections(self):
            ...

The `key` is the step's URL, so it must stay stable. Declarations are imported
automatically (see `autodiscover`).
"""
from functools import cached_property
from importlib import import_module

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import module_has_submodule

from general.onboarding.checks import MISSING, worst

DECLARATIONS_MODULE = "onboarding"

_REGISTRY = {}
_autodiscovered = False


class OnboardingStep:
    """
    One step of a company's setup, bound to that company.

    `sections()` reports what the company has and lacks; it must never change
    data. A step that can be completed in place declares a `form_class`, and
    `save()` applies a valid form. Steps that hand off to an existing screen
    just link to it from their checks.
    """

    key = None
    title = None
    description = ""
    order = 100
    # A CompanyModule value: the step is shown only to companies using it.
    # None shows it to every company.
    module = None
    # A missing check in a required step keeps the company from finishing
    # its setup; in an optional one it is only a reminder.
    required = True
    form_class = None
    submit_label = None
    # A form that is only the fallback to the step's actions starts folded
    # away under this title
    fallback_form_title = None

    def __init__(self, company):
        self.company = company

    def sections(self):
        raise NotImplementedError

    @cached_property
    def results(self):
        return self.sections()

    @property
    def status(self):
        return worst(section.status for section in self.results)

    @property
    def blocks_finish(self):
        return self.required and self.status == MISSING

    def actions(self):
        """The main ways to complete the step elsewhere: `(label, url, icon)` buttons."""
        return []

    def import_prompts(self):
        """`ImportPrompt`s for the files this step imports, to prepare them with an AI assistant."""
        return []

    def get_form(self, data=None, files=None):
        if self.form_class is None:
            return None
        return self.form_class(data=data, files=files, **self.get_form_kwargs())

    def get_form_kwargs(self):
        return {}

    def save(self, form, request):
        """Apply a valid form. Returns the message shown to the user."""
        form.save()
        return None


def register(declaration):
    """Class decorator registering an `OnboardingStep` subclass."""
    if not (isinstance(declaration, type) and issubclass(declaration, OnboardingStep)):
        raise ImproperlyConfigured(
            "@register must decorate an OnboardingStep subclass, got %r" % (declaration,)
        )
    if not declaration.key:
        raise ImproperlyConfigured("%s declares no key" % declaration.__name__)
    if not declaration.title:
        raise ImproperlyConfigured("%s declares no title" % declaration.__name__)

    registered = _REGISTRY.get(declaration.key)
    if registered is not None and registered is not declaration:
        raise ImproperlyConfigured(
            "Onboarding step '%s' is already registered by %s" % (declaration.key, registered.__name__)
        )
    _REGISTRY[declaration.key] = declaration
    return declaration


def autodiscover():
    """Import every app's `onboarding` module, once."""
    global _autodiscovered
    if _autodiscovered:
        return
    _autodiscovered = True

    for app_config in apps.get_app_configs():
        if module_has_submodule(app_config.module, DECLARATIONS_MODULE):
            import_module("%s.%s" % (app_config.name, DECLARATIONS_MODULE))


def registered_steps():
    """All registered declarations, in setup order."""
    autodiscover()
    return tuple(sorted(_REGISTRY.values(), key=lambda step: (step.order, step.key)))


class CompanyReadiness:
    """The setup steps a company is shown, and whether it can start operating."""

    def __init__(self, company):
        self.company = company
        modules = set(company.enabled_modules or ())
        self.steps = [
            declaration(company)
            for declaration in registered_steps()
            if declaration.module is None or declaration.module in modules
        ]

    def step(self, key):
        return next((step for step in self.steps if step.key == key), None)

    def is_ready(self):
        return not any(step.blocks_finish for step in self.steps)
