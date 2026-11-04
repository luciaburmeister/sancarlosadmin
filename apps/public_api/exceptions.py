"""
Every public API error has the same shape:

    {"error": {"code": "permission_denied", "message": "...", "details": {...}}}

``details`` only appears on validation errors (field → messages).
"""
import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response
from rest_framework.serializers import as_serializer_error
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Conflict."
    default_code = "conflict"


def public_api_exception_handler(exc, context):
    if isinstance(exc, DjangoValidationError):
        exc = ValidationError(detail=as_serializer_error(exc))

    response = exception_handler(exc, context)
    if response is None:
        logger.exception("Unhandled public API error", exc_info=exc)
        return Response(
            {"error": {"code": "server_error", "message": "Internal server error."}},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if isinstance(exc, ValidationError):
        error = {"code": "invalid", "message": "Invalid request.", "details": response.data}
    else:
        detail = getattr(exc, "detail", "")
        error = {
            "code": getattr(detail, "code", None) or getattr(exc, "default_code", "error"),
            "message": str(detail),
        }
    response.data = {"error": error}
    return response
