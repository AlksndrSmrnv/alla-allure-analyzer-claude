"""Custom exception hierarchy for the alla package."""


class AllaError(Exception):
    """Base exception for all alla errors."""


class ConfigurationError(AllaError):
    """Missing or invalid configuration."""


class AuthenticationError(AllaError):
    """Failed to authenticate with Allure TestOps."""


class AllureApiError(AllaError):
    """HTTP error from Allure TestOps API."""

    def __init__(self, status_code: int, message: str, endpoint: str) -> None:
        self.status_code = status_code
        self.endpoint = endpoint
        super().__init__(f"HTTP {status_code} from {endpoint}: {message}")


class PaginationLimitError(AllaError):
    """Exceeded maximum page limit (safety valve)."""
