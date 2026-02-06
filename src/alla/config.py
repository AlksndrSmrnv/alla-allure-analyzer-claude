"""Application configuration loaded from environment variables."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration for the alla application.

    All values can be set via environment variables with the ``ALLURE_`` prefix
    or through a ``.env`` file in the working directory.
    """

    model_config = SettingsConfigDict(
        env_prefix="ALLURE_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    endpoint: str = Field(description="Allure TestOps server URL")
    token: str = Field(description="API token for authentication")
    project_id: int = Field(description="Project ID in Allure TestOps")

    request_timeout: int = Field(default=30, description="HTTP request timeout in seconds")
    page_size: int = Field(default=100, description="Results per page for paginated requests")
    max_pages: int = Field(default=50, description="Safety limit on pagination iterations")

    log_level: str = Field(default="INFO", description="Logging level")
