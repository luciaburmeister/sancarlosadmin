"""
Where replies to an outgoing email land.

Nothing in the system can send `From:` a per-company address - every message
leaves through the one SMTP mailbox in `DEFAULT_FROM_EMAIL` - so `Reply-To` is
what actually decides who a client reaches when they hit Reply.

Resolution order, first hit wins:

1. the person who triggered the send, when a human did (`sender_email`)
2. the company's Reply-To for that kind of email (`company.EmailReplyTo`)
3. the company's generic Reply-To (`Company.reply_to_email`)
4. nothing - replies go to `DEFAULT_FROM_EMAIL`

Looking the address up costs a query on every send, so it must never be what
stops one: a message with the wrong Reply-To still reaches the client, while a
message that raised on the way out does not. Anything going wrong here is
logged and treated as "not configured".
"""

import logging

logger = logging.getLogger(__name__)


def sender_reply_to(user):
    """
    Reply-To for a send a specific user triggered, as a list for Celery.

    Empty when there is no user or they have no email on file, which lets the
    company configuration take over.
    """
    email = (getattr(user, "email", "") or "").strip()
    return [email] if email else []


def resolve_reply_to(company=None, email_document=None, sender_email=None):
    """
    The Reply-To addresses for one message. See the module docstring for the
    order these are tried in.
    """
    if sender_email:
        return [sender_email]

    if company is None:
        return []

    try:
        if email_document:
            from company.models import EmailReplyTo

            configured = (
                EmailReplyTo.objects
                .filter(company=company, email_document=email_document)
                .values_list("reply_to_email", flat=True)
                .first()
            )
            if configured:
                return [configured]

        generic = (getattr(company, "reply_to_email", "") or "").strip()
        return [generic] if generic else []
    except Exception:
        logger.exception(
            "Could not resolve Reply-To for %s/%s; sending without one",
            company, email_document,
        )
        return []
