import re
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

RULE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

def validate_rule_key(value):
    if not RULE_KEY_RE.match(value):
        raise ValidationError(
            _("Rule key must start with a letter and contain only letters, numbers, or underscores."))