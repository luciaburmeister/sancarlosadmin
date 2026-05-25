import base64
import hashlib

from django.core.files.base import ContentFile
from django.utils import timezone

from company.models import Company


def terms_version_for_text(text: str) -> str:
    normalized = (text or "").strip()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode()).hexdigest()[:8]


def apply_terms_acceptance(*, client, company: Company, signature_payload: str) -> None:
    """Persist signature file and immutable T&C reference for a client."""
    payload = (signature_payload or "").strip()
    if not payload:
        return

    raw = payload.split(";base64,", 1)[-1] if ";base64," in payload else payload
    image_bytes = base64.b64decode(raw)
    filename = f"client_{client.pk or 'new'}_terms.png"
    client.terms_signature.save(filename, ContentFile(image_bytes), save=False)

    snapshot = (company.terms_text or "").strip()
    client.accepted_terms_company = company
    client.accepted_terms_text = snapshot
    client.terms_accepted_at = timezone.now()
