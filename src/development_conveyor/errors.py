"""Typed failures used to keep controller stops explicit."""


class ConveyorError(RuntimeError):
    """Base controller error."""


class ConfigurationError(ConveyorError):
    """Configuration is missing, malformed, or unsafe."""


class SchemaValidationError(ConfigurationError):
    """A document does not conform to its declared schema."""


class RepositoryError(ConveyorError):
    """Repository identity or Git evidence is invalid."""


class QueueError(ConveyorError):
    """Feature queue evidence is malformed or contradictory."""


class InvalidTransition(ConveyorError):
    """A persisted state-machine transition is not allowed."""


class LockError(ConveyorError):
    """A writer or launch lock cannot be safely acquired or released."""


class AmbiguousLockError(LockError):
    """Lock ownership cannot be proven and requires human judgment."""


class SafetyViolation(ConveyorError):
    """A prohibited controller operation was requested."""


class RecoveryError(ConveyorError):
    """Durable state and externally visible evidence cannot be reconciled."""


class SessionError(ConveyorError):
    """A repository-scoped Codex session failed or returned invalid evidence."""


class RetryExhausted(ConveyorError):
    """The configured focused-attempt limit was reached."""

