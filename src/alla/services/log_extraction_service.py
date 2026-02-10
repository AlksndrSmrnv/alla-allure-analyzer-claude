"""Сервис извлечения логов из аттачментов.

Скачивает text/plain аттачменты для каждого упавшего теста и сохраняет
результат в ``FailedTestSummary.log_snippet``.

Логи, прикреплённые к тесту, считаются уже отфильтрованными по времени,
поэтому фильтрация по time-window не выполняется — весь лог передаётся целиком.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from alla.clients.base import AttachmentProvider
from alla.models.testops import AttachmentMeta, ExecutionStep, FailedTestSummary

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LogExtractionConfig:
    """Параметры извлечения логов из аттачментов."""

    max_size_kb: int = 512
    concurrency: int = 5


# ---------------------------------------------------------------------------
# LogExtractionService
# ---------------------------------------------------------------------------

class LogExtractionService:
    """Извлекает текстовые логи из аттачментов."""

    def __init__(
        self,
        provider: AttachmentProvider,
        config: LogExtractionConfig | None = None,
    ) -> None:
        self._provider = provider
        self._config = config or LogExtractionConfig()

    async def enrich_with_logs(
        self,
        summaries: list[FailedTestSummary],
    ) -> None:
        """Скачать логи для каждого теста и заполнить ``log_snippet`` in-place.

        Паттерн: semaphore + gather с per-test error resilience.

        Использует новый API:
        1. GET /api/testresult/attachment?testResultId={id} — список аттачментов
        2. GET /api/testresult/attachment/{id}/content — скачивание содержимого
        """
        if not summaries:
            return

        logger.info(
            "Логи: начало обработки %d тестов (параллелизм=%d)",
            len(summaries),
            self._config.concurrency,
        )

        semaphore = asyncio.Semaphore(self._config.concurrency)

        async def fetch_and_attach(summary: FailedTestSummary) -> None:
            # Шаг 1: получить список аттачментов для теста
            async with semaphore:
                try:
                    all_attachments = await self._provider.get_attachments_for_test_result(
                        summary.test_result_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Логи: не удалось получить список аттачментов для теста %d: %s",
                        summary.test_result_id,
                        exc,
                    )
                    return

            # Фильтрация по text/plain
            text_attachments = [
                att for att in all_attachments
                if self._is_text_attachment(att)
            ]

            if not text_attachments:
                return

            # Шаг 2: скачать содержимое каждого аттачмента
            raw_texts: list[str] = []
            for att in text_attachments:
                if att.id is None:
                    continue
                async with semaphore:
                    try:
                        content_bytes = await self._provider.get_attachment_content(
                            att.id,
                        )
                        text = content_bytes.decode("utf-8", errors="replace")
                        raw_texts.append(text)
                    except Exception as exc:
                        logger.warning(
                            "Логи: не удалось скачать аттачмент %d (%s) "
                            "для теста %d: %s",
                            att.id,
                            att.name,
                            summary.test_result_id,
                            exc,
                        )

            if not raw_texts:
                return

            combined = "\n".join(raw_texts)

            # Ограничение по размеру
            max_bytes = self._config.max_size_kb * 1024
            if len(combined.encode("utf-8")) > max_bytes:
                combined = _truncate_to_size(combined, max_bytes)

            if combined.strip():
                summary.log_snippet = combined
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Логи: тест %d — строк: %d, "
                        "полный текст:\n%s",
                        summary.test_result_id,
                        combined.count("\n") + 1,
                        combined,
                    )

        tasks = [fetch_and_attach(s) for s in summaries]
        await asyncio.gather(*tasks)

        enriched = sum(1 for s in summaries if s.log_snippet)
        logger.info("Логи: обогащено %d/%d тестов", enriched, len(summaries))

    @staticmethod
    def _is_text_attachment(att: AttachmentMeta) -> bool:
        """Проверить, является ли аттачмент текстовым (text/plain)."""
        mime = (att.type or att.content_type or "").lower()
        return mime.startswith("text/plain")

    @staticmethod
    def _collect_text_attachments(
        steps: list[ExecutionStep] | None,
    ) -> list[AttachmentMeta]:
        """Рекурсивно собрать все text/plain аттачменты из дерева шагов."""
        if not steps:
            return []

        result: list[AttachmentMeta] = []

        def walk(step_list: list[ExecutionStep]) -> None:
            for step in step_list:
                if step.attachments:
                    for att in step.attachments:
                        mime = (att.type or att.content_type or "").lower()
                        if mime.startswith("text/plain"):
                            if att.source:
                                result.append(att)
                if step.steps:
                    walk(step.steps)

        walk(steps)
        return result


def _truncate_to_size(text: str, max_bytes: int) -> str:
    """Обрезать текст, сохраняя хвост (ошибки обычно в конце лога)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text

    truncated_bytes = encoded[-max_bytes:]
    # Найти первый перенос строки, чтобы не обрезать строку посередине
    newline_idx = truncated_bytes.find(b"\n")
    if newline_idx >= 0:
        truncated_bytes = truncated_bytes[newline_idx + 1:]

    header = b"...[" + "обрезано".encode("utf-8") + b"]\n"
    return (header + truncated_bytes).decode("utf-8", errors="replace")
