"""Сервис извлечения ERROR-блоков из текстовых аттачментов.

Скачивает text/plain аттачменты для каждого упавшего теста, извлекает строки
с уровнем [ERROR] и их stack trace, помечает каждый блок именем файла-источника
и сохраняет результат в ``FailedTestSummary.log_snippet``.
"""

import asyncio
import logging
import re

from alla.clients.base import AttachmentProvider
from alla.models.testops import AttachmentMeta, FailedTestSummary

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Content type detection and text decoding
# ---------------------------------------------------------------------------

try:
    import magic as _magic
    _MAGIC_AVAILABLE = True
except ImportError:  # pragma: no cover
    _MAGIC_AVAILABLE = False

from charset_normalizer import from_bytes as _cn_from_bytes

_MAGIC_JSON_MIMES = frozenset({"application/json", "text/json", "application/x-ndjson"})
_MAGIC_XML_MIMES = frozenset({"application/xml", "text/xml"})


def _detect_content_type(content: bytes, *, fallback_mime: str = "") -> str:
    """Определить тип содержимого по байтам.

    Использует python-magic если доступен, иначе fallback_mime.
    Возвращает 'json', 'xml', 'text' или 'binary'.
    """
    if _MAGIC_AVAILABLE:
        mime: str = _magic.from_buffer(content[:2048], mime=True) or ""
    else:
        mime = fallback_mime.lower().split(";")[0].strip()

    if mime in _MAGIC_JSON_MIMES:
        return "json"
    if mime in _MAGIC_XML_MIMES:
        return "xml"
    if mime.startswith("text/"):
        # Дополнительная эвристика: text/plain может быть JSON-дампом
        stripped = content[:200].lstrip()
        if stripped.startswith(b"{") or stripped.startswith(b"["):
            return "json"
        return "text"
    if not mime:
        # Неизвестный MIME — эвристика по первым байтам
        stripped = content[:200].lstrip()
        if stripped.startswith(b"{") or stripped.startswith(b"["):
            return "json"
        if stripped.startswith(b"<"):
            return "xml"
        return "text"
    return "binary"


def _decode_text(content: bytes) -> str | None:
    """Декодировать байты в строку через charset-normalizer.

    Возвращает None если содержимое нераспознано как текст.
    """
    best = _cn_from_bytes(content).best()
    if best is None:
        return None
    return str(best)


# Regex-паттерны для HTTP-детекции в тексте
_CORR_ID_JSON_RE = re.compile(
    r"\"(?P<key>RqUID|OperUID|requestId|correlationId|traceId)\"\s*:\s*\"(?P<value>[A-Za-z0-9\-_.]{4,64})\"",
    re.IGNORECASE,
)
_CORR_ID_KV_RE = re.compile(
    r"(?P<key>RqUID|OperUID|requestId|correlationId|traceId)"
    r"\s*[=:]\s*\"?(?P<value>[A-Za-z0-9\-_.]{4,64})",
    re.IGNORECASE,
)
_CORR_ID_XML_RE = re.compile(
    r"<(?P<key>RqUID|OperUID|requestId|correlationId|traceId)>"
    r"(?P<value>[A-Za-z0-9\-_.]{4,64})"
    r"</(?:RqUID|OperUID|requestId|correlationId|traceId)>",
    re.IGNORECASE,
)
_HTTP_STATUS_RE = re.compile(r"HTTP/[12](?:\.\d)?\s+(4\d\d|5\d\d)\b")
_STATUS_CODE_RE = re.compile(
    r"\"(?P<key>statusCode|status|code)\"\s*:\s*(?P<value>[45]\d{2})",
    re.IGNORECASE,
)
_ERROR_FIELD_RE = re.compile(
    r"\"(?P<key>error(?:Code|Message)?|fault(?:Code|String)?|cause|reason)"
    r"\"\s*:\s*\"(?P<value>[^\"]{1,300})\"",
    re.IGNORECASE,
)
_CONTEXT_FIELD_RE = re.compile(
    r"\"(?P<key>message|description|details)\"\s*:\s*\"(?P<value>[^\"]{1,300})\"",
    re.IGNORECASE,
)
_XML_ERROR_RE = re.compile(
    r"<(?P<key>fault(?:Code|String)?|error(?:Code)?|errorMessage)"
    r">(?P<value>[^<]{1,300})</",
    re.IGNORECASE,
)


def _extract_text_http_info(text: str) -> str:
    """Извлечь HTTP-контекст из сырого текста через regex.

    Возвращает форматированную строку или пустую строку если ничего не найдено.
    """
    corr_ids: dict[str, str] = {}
    http_statuses: list[str] = []
    error_fields: dict[str, str] = {}
    context_fields: dict[str, str] = {}
    has_error_signal = False

    for m in _CORR_ID_JSON_RE.finditer(text):
        corr_ids.setdefault(m.group("key"), m.group("value"))
    for m in _CORR_ID_KV_RE.finditer(text):
        corr_ids.setdefault(m.group("key"), m.group("value"))
    for m in _CORR_ID_XML_RE.finditer(text):
        corr_ids.setdefault(m.group("key"), m.group("value"))

    for m in _HTTP_STATUS_RE.finditer(text):
        status = m.group(1)
        if status not in http_statuses:
            http_statuses.append(status)
        has_error_signal = True

    for m in _STATUS_CODE_RE.finditer(text):
        status = m.group("value")
        if status not in http_statuses:
            http_statuses.append(status)
        has_error_signal = True

    for m in _ERROR_FIELD_RE.finditer(text):
        value = m.group("value").strip()
        if value:
            error_fields[m.group("key")] = value
            has_error_signal = True

    for m in _XML_ERROR_RE.finditer(text):
        value = m.group("value").strip()
        if value:
            error_fields.setdefault(m.group("key"), value)
            has_error_signal = True

    for m in _CONTEXT_FIELD_RE.finditer(text):
        value = m.group("value").strip()
        if value:
            context_fields[m.group("key")] = value

    if not corr_ids and not has_error_signal:
        return ""

    lines: list[str] = []
    if corr_ids:
        lines.append("Корреляция: " + ", ".join(f"{k}={v}" for k, v in corr_ids.items()))
    for status in http_statuses:
        lines.append(f"HTTP статус: {status}")
    for key, value in error_fields.items():
        lines.append(f"{key}: {value}")
    if has_error_signal:
        for key, value in context_fields.items():
            lines.append(f"{key}: {value}")

    return "\n".join(lines)


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
