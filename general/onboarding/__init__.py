"""
The setup a new company goes through before it can operate.

Apps declare their steps in `<app>/onboarding.py`; each step reports checks
(ok / warning / missing) and may offer a form to complete itself in place.
See `registry.py` for the how-to.
"""
from general.onboarding.checks import MISSING, OK, WARNING, Check, Section
from general.onboarding.prompts import Column, ImportPrompt
from general.onboarding.registry import CompanyReadiness, OnboardingStep, register, registered_steps

__all__ = [
    "MISSING", "OK", "WARNING", "Check", "Section", "Column", "ImportPrompt",
    "CompanyReadiness", "OnboardingStep", "register", "registered_steps",
]
