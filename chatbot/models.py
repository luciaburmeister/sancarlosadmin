"""
Chatbot permissions model.

Provides a lightweight model solely to host custom Django permissions
for the chatbot module.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class ChatbotPermission(models.Model):
    """Dummy model that exists only to register chatbot permissions.

    No database table is created (``managed = False``).
    """

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ("use_chatbot", _("Can use the ERP chatbot")),
        ]
