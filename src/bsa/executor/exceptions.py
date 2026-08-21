class BsaError(Exception):
    """Base class for all Branch Sync Agent errors."""


class DomainError(BsaError):
    """Business-state error; handled via state transitions, not raised."""


class InfrastructureError(BsaError):
    """Network/IO/timeout error; caught at node boundaries into state.errors."""


class SafetyViolation(BsaError):
    """Safety red-line hit; forces rejection for manual review."""
