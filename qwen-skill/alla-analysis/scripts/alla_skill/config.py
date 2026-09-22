"""Only configuration required by a standalone read-only analysis."""

import os
import math
import subprocess
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
    max_detail_enrichments: int = 100
    clustering_threshold: float = 0.60
    clustering_step_strict_threshold: float = 0.95
    logs_clustering_weight: float = 0.15

    def __post_init__(self):
        url = urlsplit(self.endpoint)
        if (
            url.scheme not in ("http", "https")
            or not url.netloc
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            raise ValueError("ALLURE_ENDPOINT должен быть HTTP(S) URL без пароля и query")
        if not self.token:
            raise ValueError("Задайте ALLURE_TOKEN в окружении или приватном .env скилла")
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
            or self.max_detail_enrichments < 0
        ):
            raise ValueError("Некорректные ограничения запросов")

        for value in (
            self.clustering_threshold,
            self.clustering_step_strict_threshold,
            self.logs_clustering_weight,
        ):
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("Пороги и веса кластеризации должны быть в диапазоне 0..1")

    def clustering_config(self):
        from .services.clustering_service import ClusteringConfig

        return ClusteringConfig(
            similarity_threshold=self.clustering_threshold,
            step_path_strict_threshold=self.clustering_step_strict_threshold,
            log_similarity_weight=self.logs_clustering_weight,
        )

    @classmethod
    def load(cls, project_root: Path, env_file: Path | None = None):
        # Never implicitly load the target project's potentially tracked .env.
        selected = (
            Path(project_root) / env_file
            if env_file is not None
            else Path(__file__).resolve().parents[2] / ".env"
        ).resolve()
        if env_file is not None and not selected.is_file():
            raise ValueError("Указанный env-файл не найден")
        file_values = dotenv_values(selected) if selected.is_file() else {}
        if file_values.get("ALLURE_TOKEN"):
            try:
                tracked = (
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(selected.parent),
                            "ls-files",
                            "--error-unmatch",
                            "--",
                            selected.name,
                        ],
                        capture_output=True,
                        timeout=10,
                    ).returncode
                    == 0
                )
            except (OSError, subprocess.TimeoutExpired):
                raise ValueError(
                    "Не удалось проверить env-файл в Git; используйте переменные окружения"
                ) from None
            if tracked:
                raise ValueError(
                    "Env-файл с ALLURE_TOKEN отслеживается Git; используйте приватный файл или окружение"
                )
        values = {**file_values, **os.environ}

        def number(name, default, convert):
            try:
                return convert(values.get(name, default))
            except (TypeError, ValueError):
                kind = "целое число" if convert is int else "число"
                raise ValueError(f"{name}: требуется {kind}; пустое значение недопустимо") from None

        return cls(
            endpoint=(values.get("ALLURE_ENDPOINT") or "").strip().rstrip("/"),
            token=values.get("ALLURE_TOKEN") or "",
            max_detail_enrichments=number("ALLURE_MAX_DETAIL_ENRICHMENTS", "100", int),
            clustering_threshold=number("ALLURE_CLUSTERING_THRESHOLD", "0.60", float),
            clustering_step_strict_threshold=number(
                "ALLURE_CLUSTERING_STEP_STRICT_THRESHOLD", "0.95", float
            ),
            logs_clustering_weight=number("ALLURE_LOGS_CLUSTERING_WEIGHT", "0.15", float),
        )
