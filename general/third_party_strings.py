"""
Strings used by third-party packages that ship no Spanish catalog.

They are marked here only so ``makemessages`` keeps them in our
locale/es catalog, which Django consults before the packages' own.
"""

from django.utils.translation import gettext_noop

THIRD_PARTY_STRINGS = [
    # unfold: blank choice / empty label of admin select widgets
    gettext_noop("Select value"),
]
