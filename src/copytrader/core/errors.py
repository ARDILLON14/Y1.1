"""Exception hierarchy. Callers branch on type, never on message text."""

from __future__ import annotations


class CopyTraderError(Exception):
    """Base class for all application errors."""


class ConfigError(CopyTraderError):
    pass


class ProviderError(CopyTraderError):
    """An external dependency failed. ``retryable`` drives the retry policy."""

    def __init__(self, message: str, *, provider: str = "", retryable: bool = True,
                 status_code: int | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable
        self.status_code = status_code


class RateLimitedError(ProviderError):
    def __init__(self, message: str, *, provider: str = "", retry_after: float | None = None) -> None:
        super().__init__(message, provider=provider, retryable=True, status_code=429)
        self.retry_after = retry_after


class CircuitOpenError(ProviderError):
    """Raised immediately while a circuit breaker is open (fail fast)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"circuit '{name}' is open", provider=name, retryable=False)


class DataIncompleteError(CopyTraderError):
    """Data needed for a safe decision is missing or stale."""


class ParseError(CopyTraderError):
    """A transaction could not be interpreted unambiguously."""


class ExecutionError(CopyTraderError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class SecurityError(CopyTraderError):
    pass


class SignerPolicyViolation(SecurityError):
    """The signer refused to sign a transaction that violates its policy."""


class AuthError(SecurityError):
    pass
