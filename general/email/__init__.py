"""
Per-document-type Reply-To configuration for outgoing email.

Apps declare the kinds of email they send in `<app>/email_documents.py`, call
sites tag each send with `EmailService.send_email(email_document="<key>")`, and
each company configures where replies to that kind of email should go. See
`registry.py` for the how-to and `reply_to.py` for the resolution order.
"""

from .registry import (
    EmailDocument,
    autodiscover,
    document_choices,
    document_label,
    is_registered,
    register,
    registered_documents,
)
from .reply_to import resolve_reply_to, sender_cc, sender_reply_to

__all__ = [
    "EmailDocument",
    "autodiscover",
    "document_choices",
    "document_label",
    "is_registered",
    "register",
    "registered_documents",
    "resolve_reply_to",
    "sender_cc",
    "sender_reply_to",
]
