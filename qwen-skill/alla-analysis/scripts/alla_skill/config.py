"""Only configuration required by a standalone read-only analysis."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    endpoint: str
    token: str = field(repr=False)
    detail_concurrency: int = 10
    page_size: int = 100
    max_pages: int = 10000
    max_attachment_bytes: int = 10 * 1024 * 1024
    retries: int = 3
    request_timeout: int = 30

    def __post_init__(self):
        url = urlsplit(self.endpoint)
        if (
            url.scheme not in ("http", "https")
            or not url.netloc
            or url.username
            or url.query
            or url.fragment
        ):
            raise ValueError("ALLURE_ENDPOINT должен быть HTTP(S) URL без пароля и query")
        if not self.token:
            raise ValueError("Задайте ALLURE_TOKEN в окружении или .env проекта")
        if (
            min(
                self.detail_concurrency,
                self.page_size,
                self.max_pages,
                self.max_attachment_bytes,
                self.request_timeout,
            )
            < 1
            or self.retries < 0
        ):
            raise ValueError("Некорректные ограничения запросов")

    @classmethod
    def load(cls, project_root: Path):
        values = {**dotenv_values(project_root / ".env"), **os.environ}
        return cls(
            endpoint=(values.get("ALLURE_ENDPOINT") or "").strip().rstrip("/"),
            token=values.get("ALLURE_TOKEN") or "",
        )
