"""Shared safe service errors for REST, MCP, and mediated Python calls."""

from pydantic import ValidationError

from .access import AuthenticationError, Forbidden, IdentityUnavailable
from .cache import DataBusy
from .lifecycle import Conflict, IntegrityError, NotFound
from .quotas import QuotaExceeded
from .request_limits import RateLimited, RequestCapacity
from .service_pool import ServiceFailure


def problem(error: Exception) -> tuple[int, dict]:
    if isinstance(error, ServiceFailure):
        return error.status, error.body
    if isinstance(error, IntegrityError):
        status, code, message = 500, "internal_error", "request could not be completed"
    elif isinstance(error, AuthenticationError):
        status, code, message = 401, "authentication_required", str(error)
    elif isinstance(error, Forbidden):
        status, code, message = 403, "permission_denied", "permission denied"
    elif isinstance(error, NotFound):
        status, code, message = 404, "not_found", str(error)
    elif isinstance(error, Conflict):
        status, code, message = 409, "revision_conflict", str(error)
    elif isinstance(error, IdentityUnavailable):
        status, code, message = 503, "identity_unavailable", "identity verification is unavailable"
    elif isinstance(error, DataBusy):
        status, code, message = 503, "data_busy", str(error)
    elif isinstance(error, RequestCapacity):
        return 503, {
            "error": {
                "code": "request_capacity",
                "message": str(error),
                "retry_after": error.retry_after,
            }
        }
    elif isinstance(error, RateLimited):
        return 429, {
            "error": {
                "code": "rate_limited",
                "message": str(error),
                "limit": error.limit,
                "retry_after": error.retry_after,
            }
        }
    elif isinstance(error, QuotaExceeded):
        return 429, {
            "error": {
                "code": "quota_exceeded",
                "message": str(error),
                "quota": error.quota,
                "retry_after": error.retry_after,
            }
        }
    elif isinstance(error, ValidationError):
        return 422, {
            "error": {
                "code": "invalid_request",
                "message": "request validation failed",
                "fields": [
                    {"path": list(item["loc"]), "message": item["msg"]}
                    for item in error.errors()[:100]
                ],
            }
        }
    elif isinstance(error, ValueError):
        status, code, message = 422, "invalid_request", str(error)
    else:
        status, code, message = 500, "internal_error", "request could not be completed"
    return status, {"error": {"code": code, "message": message}}
