"""Сервис извлечения и фильтрации логов из аттачментов execution-шагов.

Скачивает text/plain аттачменты для каждого упавшего теста, парсит timestamps
из строк лога, фильтрует по time-window теста (± буфер) и сохраняет
результат в ``FailedTestSummary.log_snippet``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from alla.clients.base import AttachmentProvider
from alla.models.testops import AttachmentMeta, ExecutionStep, FailedTestSummary

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LogExtractionConfig:
    """Параметры извлечения логов из аттачментов."""

    time_buffer_sec: int = 30
    max_size_kb: int = 512
    concurrency: int = 5


# ---------------------------------------------------------------------------
# Timestamp parsing — мульти-формат парсер для строк лога
# ---------------------------------------------------------------------------

# ISO 8601 datetime: 2026-02-09T10:23:45.123Z, 2026-02-09 10:23:45,123+03:00
_TS_ISO_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
    r"(?:[.,](\d{1,6}))?"
    r"(Z|[+-]\d{2}:?\d{2})?"
)

# Log4j/Logback: 2026-02-09 10:23:45,123 (без timezone)
_TS_LOG4J_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r"(?:,(\d{1,3}))?"
)

# Epoch milliseconds в начале строки: 1707473025123
_TS_EPOCH_MS_RE = re.compile(r"^(\d{13})\b")

_TS_FMT_ISO = "%Y-%m-%d %H:%M:%S"


def _parse_timestamp_ms(line: str) -> int | None:
    """Попытаться извлечь timestamp (epoch ms) из строки лога.

    Пробует несколько распространённых форматов в порядке приоритета.
    Возвращает None если не удалось распознать ни один.
    """
    # 1. Epoch ms в начале строки (самый быстрый)
    m = _TS_EPOCH_MS_RE.match(line)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, OverflowError):
            pass

    # 2. ISO 8601
    m = _TS_ISO_RE.search(line)
    if m:
        ts = _parse_datetime_group(m.group(1), m.group(2), m.group(3))
        if ts is not None:
            return ts

    # 3. Log4j format (без T-separator, с запятой вместо точки)
    m = _TS_LOG4J_RE.search(line)
    if m:
        ts = _parse_datetime_group(m.group(1), m.group(2))
        if ts is not None:
            return ts

    return None


def _parse_datetime_group(
    base: str,
    frac: str | None,
    tz_str: str | None = None,
) -> int | None:
    """Распарсить datetime-строку с опциональной дробной частью секунд и timezone."""
    try:
        normalized = base.replace("T", " ")
        dt = datetime.strptime(normalized, _TS_FMT_ISO)

        # Определяем timezone
        if tz_str is None or tz_str == "" or tz_str == "Z":
            tz = timezone.utc
        else:
            # Парсим +HH:MM, +HHMM, -HH:MM, -HHMM
            sign = 1 if tz_str[0] == "+" else -1
            tz_digits = tz_str[1:].replace(":", "")
            hours = int(tz_digits[:2])
            minutes = int(tz_digits[2:4]) if len(tz_digits) >= 4 else 0
            offset = timedelta(hours=sign * hours, minutes=sign * minutes)
            tz = timezone(offset)

        dt = dt.replace(tzinfo=tz)
        ms = int(dt.timestamp() * 1000)
        if frac:
            # Дробная часть может быть 1-6 знаков; приводим к миллисекундам
            frac_padded = frac.ljust(3, "0")[:3]
            ms += int(frac_padded)
        return ms
    except (ValueError, OverflowError):
        return None


# ---------------------------------------------------------------------------
# LogExtractionService
# ---------------------------------------------------------------------------

class LogExtractionService:
    """Извлекает текстовые логи из аттачментов и фильтрует по time-window."""

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

        async def fetch_and_filter(summary: FailedTestSummary) -> None:
            # Шаг 1: получить список аттачментов для теста
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

            filtered = self._filter_by_time_window(
                combined,
                test_start_ms=summary.test_start_ms,
                duration_ms=summary.duration_ms,
            )

            max_bytes = self._config.max_size_kb * 1024
            if len(filtered.encode("utf-8")) > max_bytes:
                filtered = _truncate_to_size(filtered, max_bytes)

            if filtered.strip():
                summary.log_snippet = filtered

        tasks = [fetch_and_filter(s) for s in summaries]
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

    def _filter_by_time_window(
        self,
        log_text: str,
        test_start_ms: int | None,
        duration_ms: int | None,
    ) -> str:
        """Оставить только строки лога, попадающие в time-window теста.

        Окно: ``[test_start - buffer, test_start + duration + buffer]``.

        Если start/duration неизвестны, или ни одна строка не содержит
        распознаваемой метки времени, возвращает весь текст как есть.
        """
        if test_start_ms is None or duration_ms is None:
            return log_text

        buffer_ms = self._config.time_buffer_sec * 1000
        window_start = test_start_ms - buffer_ms
        window_end = test_start_ms + duration_ms + buffer_ms

        lines = log_text.splitlines()
        filtered: list[str] = []
        in_window = False
        any_timestamp_found = False

        for line in lines:
            ts_ms = _parse_timestamp_ms(line)
            if ts_ms is not None:
                any_timestamp_found = True
                in_window = window_start <= ts_ms <= window_end
                if in_window:
                    filtered.append(line)
            else:
                # Строки без timestamp сохраняются если предыдущая строка
                # была в окне (continuation: stack traces, multi-line сообщения)
                if in_window:
                    filtered.append(line)

        if not any_timestamp_found:
            return log_text

        return "\n".join(filtered)


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

    header = b"...[" + "\u043e\u0431\u0440\u0435\u0437\u0430\u043d\u043e".encode("utf-8") + b"]\n"
    return (header + truncated_bytes).decode("utf-8", errors="replace")
