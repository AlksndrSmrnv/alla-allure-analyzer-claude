"""Сервис извлечения ERROR-блоков из текстовых аттачментов.

Скачивает text/plain аттачменты для каждого упавшего теста, извлекает строки
с уровнем [ERROR] и их stack trace, помечает каждый блок именем файла-источника
и сохраняет результат в ``FailedTestSummary.log_snippet``.
"""

from __future__ import annotations

import asyncio
import logging
import re

from alla.clients.base import AttachmentProvider
from alla.models.testops import AttachmentMeta, FailedTestSummary

logger = logging.getLogger(__name__)


class LogExtractionConfig:
    """Параметры извлечения логов из аттачментов."""

    def __init__(self, *, concurrency: int = 5) -> None:
        self.concurrency = concurrency


# ---------------------------------------------------------------------------
# ERROR-block extraction
# ---------------------------------------------------------------------------

# Паттерн для определения начала новой лог-записи (строка с датой/временем).
# Матчит форматы: 2026-02-09T10:23:45, 2026-02-09 10:23:45
_LOG_LINE_START_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
)

# Паттерн для обнаружения [ERROR] (case-insensitive) в квадратных скобках.
_ERROR_LEVEL_RE = re.compile(r"\[error\]", re.IGNORECASE)


def _extract_error_blocks(log_text: str) -> str:
    """Извлечь блоки ERROR из текста лога.

    Логика:
    1. Найти строку, содержащую ``[ERROR]`` (case-insensitive).
    2. Захватить эту строку и все последующие строки, которые являются
       «продолжением» (stack trace) — т.е. **не** начинаются с timestamp
       нового лог-сообщения.
    3. Если следующая строка начинается с timestamp, но тоже содержит
       ``[ERROR]``, она становится началом нового ERROR-блока.

    Returns:
        Объединённые ERROR-блоки. Пустая строка, если ни одного не найдено.
    """
    lines = log_text.splitlines()
    blocks: list[str] = []
    current_block: list[str] = []
    in_error_block = False

    for line in lines:
        is_new_log_entry = bool(_LOG_LINE_START_RE.match(line))
        is_error = bool(_ERROR_LEVEL_RE.search(line))

        if is_error and is_new_log_entry:
            # Новая строка [ERROR] — начало нового блока.
            # Сохраняем предыдущий блок, если был.
            if current_block:
                blocks.append("\n".join(current_block))
            current_block = [line]
            in_error_block = True
        elif in_error_block:
            if is_new_log_entry and not is_error:
                # Новая лог-запись без [ERROR] — конец текущего блока.
                blocks.append("\n".join(current_block))
                current_block = []
                in_error_block = False
            else:
                # Continuation (stack trace) или ещё один [ERROR] без timestamp.
                current_block.append(line)

    # Финальный блок
    if current_block:
        blocks.append("\n".join(current_block))

    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# LogExtractionService
# ---------------------------------------------------------------------------

class LogExtractionService:
    """Извлекает ERROR-блоки из текстовых аттачментов."""

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

        Для каждого теста:
        1. GET /api/testresult/attachment?testResultId={id} — список аттачментов
        2. GET /api/testresult/attachment/{id}/content — скачивание содержимого
        3. Извлечение ERROR-блоков из каждого аттачмента
        4. Склейка блоков с пометкой источника
        """
        if not summaries:
            return

        logger.info(
            "Логи: начало обработки %d тестов (параллелизм=%d)",
            len(summaries),
            self._config.concurrency,
        )

        semaphore = asyncio.Semaphore(self._config.concurrency)

        async def fetch_and_extract(summary: FailedTestSummary) -> None:
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

            # Шаг 2: скачать содержимое и извлечь ERROR-блоки
            all_error_sections: list[str] = []

            for att in text_attachments:
                if att.id is None:
                    continue
                async with semaphore:
                    try:
                        content_bytes = await self._provider.get_attachment_content(
                            att.id,
                        )
                        text = content_bytes.decode("utf-8", errors="replace")
                    except Exception as exc:
                        logger.warning(
                            "Логи: не удалось скачать аттачмент %d (%s) "
                            "для теста %d: %s",
                            att.id,
                            att.name,
                            summary.test_result_id,
                            exc,
                        )
                        continue

                error_blocks = _extract_error_blocks(text)
                if error_blocks.strip():
                    att_name = att.name or f"attachment-{att.id}"
                    header = f"--- [файл: {att_name}] ---"
                    all_error_sections.append(f"{header}\n{error_blocks}")

            if not all_error_sections:
                return

            combined = "\n\n".join(all_error_sections)
            summary.log_snippet = combined

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Логи: тест %d — извлечено ERROR-блоков из %d файлов, "
                    "общий размер: %d символов",
                    summary.test_result_id,
                    len(all_error_sections),
                    len(combined),
                )

        tasks = [fetch_and_extract(s) for s in summaries]
        await asyncio.gather(*tasks)

        enriched = sum(1 for s in summaries if s.log_snippet)
        logger.info("Логи: обогащено %d/%d тестов", enriched, len(summaries))

    @staticmethod
    def _is_text_attachment(att: AttachmentMeta) -> bool:
        """Проверить, является ли аттачмент текстовым (text/plain)."""
        mime = (att.type or att.content_type or "").lower()
        return mime.startswith("text/plain")
