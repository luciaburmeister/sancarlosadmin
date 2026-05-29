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


def client_terms_status(*, client, company: Company) -> dict:
    """
    - needs_terms_signature: no T&C configured → False; never signed → True;
      signed but company terms changed → True.
    """
    current_v = terms_version_for_text((company.terms_text or "").strip())
    accepted_v = client.terms_version or ""
    has_sig = bool(client.terms_signature)
    if not current_v:
        needs = False
    elif not has_sig:
        needs = True
    else:
        needs = accepted_v != current_v
    return {
        "has_signature": has_sig,
        "terms_version": accepted_v,
        "current_terms_version": current_v,
        "needs_terms_signature": needs,
        "terms_outdated": has_sig and bool(current_v) and accepted_v != current_v,
    }


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
