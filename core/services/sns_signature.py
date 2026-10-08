"""Validation helpers for Amazon SNS HTTPS notifications."""

import base64
import hashlib
import re
from functools import lru_cache
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, build_opener

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from django.core.cache import cache


class InvalidSNSMessage(ValueError):
    pass


_SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?$")
_CERT_FILE = re.compile(r"^SimpleNotificationService-[A-Za-z0-9_-]+\.pem$")


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_HTTPS_OPENER = build_opener(_NoRedirectHandler)


def _validated_sns_url(value, *, certificate=False):
    try:
        parsed = urlparse(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise InvalidSNSMessage("Invalid SNS URL") from exc

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not _SNS_HOST.fullmatch(parsed.hostname)
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise InvalidSNSMessage("Untrusted SNS URL")
    if certificate:
        filename = parsed.path.rsplit("/", 1)[-1]
        if parsed.query or not _CERT_FILE.fullmatch(filename):
            raise InvalidSNSMessage("Untrusted SNS certificate URL")
    return value


# AWS rotates signing certs under a new filename, so a cached URL never changes.
_CERT_CACHE_TIMEOUT = 24 * 60 * 60


@lru_cache(maxsize=8)
def _download_certificate(url):
    """Fetch a signing cert once for all workers; recycled workers start cold."""
    _validated_sns_url(url, certificate=True)
    key = "sns-cert:" + hashlib.sha256(url.encode()).hexdigest()
    pem = cache.get(key)
    if pem is None:
        with _HTTPS_OPENER.open(url, timeout=5) as response:  # noqa: S310
            pem = response.read()
        cache.set(key, pem, timeout=_CERT_CACHE_TIMEOUT)
    return pem


def _canonical_message(message):
    message_type = message.get("Type")
    if message_type == "Notification":
        fields = ["Message", "MessageId"]
        if "Subject" in message:
            fields.append("Subject")
        fields.extend(["Timestamp", "TopicArn", "Type"])
    elif message_type in ("SubscriptionConfirmation", "UnsubscribeConfirmation"):
        fields = [
            "Message", "MessageId", "SubscribeURL", "Timestamp", "Token",
            "TopicArn", "Type",
        ]
    else:
        raise InvalidSNSMessage("Unsupported SNS message type")

    try:
        return "".join(f"{field}\n{message[field]}\n" for field in fields)
    except KeyError as exc:
        raise InvalidSNSMessage(f"Missing SNS field: {exc.args[0]}") from exc


def verify_sns_message(message, *, expected_topic_arn):
    """Verify topic, certificate origin and RSA signature of an SNS envelope."""
    if not expected_topic_arn or message.get("TopicArn") != expected_topic_arn:
        raise InvalidSNSMessage("Unexpected SNS topic")

    signature_version = message.get("SignatureVersion")
    if signature_version == "1":
        algorithm = hashes.SHA1()
    elif signature_version == "2":
        algorithm = hashes.SHA256()
    else:
        raise InvalidSNSMessage("Unsupported SNS signature version")

    try:
        signature = base64.b64decode(message["Signature"], validate=True)
        certificate = x509.load_pem_x509_certificate(
            _download_certificate(message["SigningCertURL"])
        )
        certificate.public_key().verify(
            signature,
            _canonical_message(message).encode("utf-8"),
            padding.PKCS1v15(),
            algorithm,
        )
    except InvalidSNSMessage:
        raise
    except Exception as exc:
        raise InvalidSNSMessage("Invalid SNS signature") from exc


def confirm_sns_subscription(message):
    """Confirm a verified SNS subscription using its allow-listed AWS URL."""
    url = _validated_sns_url(message.get("SubscribeURL", ""))
    with _HTTPS_OPENER.open(url, timeout=5) as response:  # noqa: S310
        response.read()
