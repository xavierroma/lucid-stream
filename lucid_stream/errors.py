class ConfigError(Exception):
    """Raised when CLI or environment configuration is invalid."""


class RetryableError(Exception):
    """Raised for transient failures that should trigger a retry."""


class GracefulStop(Exception):
    """Raised when shutdown is requested."""
