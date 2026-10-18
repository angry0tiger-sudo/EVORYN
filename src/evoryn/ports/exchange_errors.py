"""Provider-independent error taxonomy for exchange integrations."""

from __future__ import annotations

from enum import StrEnum


class ExchangeErrorCode(StrEnum):
    """Stable machine-readable exchange failure categories."""

    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    UNAVAILABLE = "unavailable"
    MALFORMED_RESPONSE = "malformed_response"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    TRADING_DISABLED = "trading_disabled"
    UNKNOWN_OUTCOME = "unknown_outcome"


class ExchangeError(RuntimeError):
    """Base exception carrying stable, provider-independent failure semantics."""

    code: ExchangeErrorCode = ExchangeErrorCode.TRANSPORT
    retryable: bool = False

    def __init__(self, message: str) -> None:
        """Create an exchange error without retaining request secrets or raw payloads."""
        super().__init__(message)


class AuthenticationError(ExchangeError):
    """Credentials are missing, invalid, or rejected by the provider."""

    code = ExchangeErrorCode.AUTHENTICATION


class AuthorizationError(ExchangeError):
    """Credentials are valid but lack the required read permission."""

    code = ExchangeErrorCode.AUTHORIZATION


class InvalidExchangeRequest(ExchangeError):
    """A request parameter violates a provider or common contract."""

    code = ExchangeErrorCode.INVALID_REQUEST


class ExchangeResourceNotFound(ExchangeError):
    """The requested exchange resource does not exist."""

    code = ExchangeErrorCode.NOT_FOUND


class ExchangeConflict(ExchangeError):
    """The provider reports a conflicting or invalid resource state."""

    code = ExchangeErrorCode.CONFLICT


class RateLimitExceeded(ExchangeError):
    """A local or provider rate limit was hit; callers can inspect retry-after."""

    code = ExchangeErrorCode.RATE_LIMIT
    retryable = True

    def __init__(self, message: str, *, retry_after_seconds: float) -> None:
        """Store a non-negative retry delay without automatically retrying a request."""
        super().__init__(message)
        self.retry_after_seconds = max(0.0, retry_after_seconds)


class ExchangeTimeout(ExchangeError):
    """The exchange request exceeded its configured timeout."""

    code = ExchangeErrorCode.TIMEOUT
    retryable = True


class ExchangeTransportError(ExchangeError):
    """Network transport failed before a trustworthy response was available."""

    code = ExchangeErrorCode.TRANSPORT
    retryable = True


class ExchangeUnavailable(ExchangeError):
    """The provider is temporarily unavailable."""

    code = ExchangeErrorCode.UNAVAILABLE
    retryable = True


class MalformedExchangeResponse(ExchangeError):
    """A response violated the normalized exchange contract."""

    code = ExchangeErrorCode.MALFORMED_RESPONSE


class UnsupportedExchangeCapability(ExchangeError):
    """The selected adapter does not declare or implement the requested capability."""

    code = ExchangeErrorCode.UNSUPPORTED_CAPABILITY


class TradingDisabledError(ExchangeError):
    """Trading writes are intentionally disabled by the current project phase."""

    code = ExchangeErrorCode.TRADING_DISABLED


class OrderOutcomeUnknown(ExchangeError):
    """A write outcome cannot be confirmed and must not be blindly retried."""

    code = ExchangeErrorCode.UNKNOWN_OUTCOME
    retryable = False
