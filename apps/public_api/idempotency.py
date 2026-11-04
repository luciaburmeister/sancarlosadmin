"""
``Idempotency-Key`` support for POST handlers.

The first request with a key runs and, when it succeeds (2xx), its response is
stored. Repeating the same request with the same key within 24 hours returns
that stored response (header ``Idempotent-Replayed: true``) without running
again. The same key with a different body, or while the first request is
still running, is a 409. A failed request stores nothing, so it can be retried
with the same key.
"""
import hashlib
import json
from datetime import timedelta
from functools import wraps

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.public_api.exceptions import Conflict
from apps.public_api.models import IdempotencyRecord

HEADER = "Idempotency-Key"
TTL = timedelta(hours=24)


def _request_hash(request) -> str:
    payload = json.dumps(
        {"method": request.method, "path": request.path, "data": request.data},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _claim(api_key, key: str, request_hash: str):
    """The record for ``key``: (record, created). Expired records are dropped first."""
    IdempotencyRecord.objects.filter(api_key=api_key, created_at__lt=timezone.now() - TTL).delete()
    try:
        # Savepoint: a duplicate must not break an enclosing transaction
        with transaction.atomic():
            return IdempotencyRecord.objects.create(api_key=api_key, key=key, request_hash=request_hash), True
    except IntegrityError:
        # Another request with this key got there first
        return IdempotencyRecord.objects.get(api_key=api_key, key=key), False


def idempotent(handler):
    @wraps(handler)
    def wrapper(view, request, *args, **kwargs):
        key = request.headers.get(HEADER)
        if not key:
            return handler(view, request, *args, **kwargs)
        if len(key) > 255:
            raise ValidationError({HEADER: ["Must be 255 characters or fewer."]})

        request_hash = _request_hash(request)
        record, created = _claim(request.auth, key, request_hash)
        if not created:
            if record.request_hash != request_hash:
                raise Conflict(
                    "This Idempotency-Key was already used with a different request.",
                    code="idempotency_key_reused",
                )
            if record.response_status is None:
                raise Conflict(
                    "A request with this Idempotency-Key is still being processed.",
                    code="idempotency_request_in_progress",
                )
            return Response(
                record.response_body,
                status=record.response_status,
                headers={"Idempotent-Replayed": "true"},
            )

        try:
            response = handler(view, request, *args, **kwargs)
        except Exception:
            record.delete()
            raise
        if 200 <= response.status_code < 300:
            record.response_status = response.status_code
            record.response_body = response.data
            record.save(update_fields=["response_status", "response_body"])
        else:
            record.delete()
        return response

    return wrapper
