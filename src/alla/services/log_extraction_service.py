"""Сервис извлечения ERROR-блоков из текстовых аттачментов.

Скачивает text/plain аттачменты для каждого упавшего теста, извлекает строки
с уровнем [ERROR] и их stack trace, помечает каждый блок именем файла-источника
и сохраняет результат в ``FailedTestSummary.log_snippet``.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

import ijson

from alla.clients.base import AttachmentProvider
from alla.models.testops import AttachmentMeta, FailedTestSummary
from alla.utils.log_utils import format_correlation_pairs

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


@dataclass
class _HttpSignals:
    """Собранные HTTP-сигналы из одного документа/объекта."""

    corr_ids: dict[str, str] = field(default_factory=dict)
    http_statuses: list[str] = field(default_factory=list)
    error_fields: dict[str, str] = field(default_factory=dict)
    context_fields: dict[str, str] = field(default_factory=dict)

    @property
    def has_error_signal(self) -> bool:
        return bool(self.http_statuses) or bool(self.error_fields)


def _format_http_info(signals: _HttpSignals) -> str:
    """Сформировать человекочитаемый HTTP-блок только при наличии error-signal."""
    if not signals.has_error_signal:
        return ""

    lines: list[str] = []
    if signals.corr_ids:
        lines.append(
            "Корреляция: "
            + ", ".join(f"{k}={v}" for k, v in signals.corr_ids.items())
        )
    for status in signals.http_statuses:
        lines.append(f"HTTP статус: {status}")
    for key, value in signals.error_fields.items():
        lines.append(f"{key}: {value}")
    for key, value in signals.context_fields.items():
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _format_correlation_hint(signals: _HttpSignals) -> str | None:
    """Сформировать опорную correlation-строку для UI/кластера."""
    return format_correlation_pairs(signals.corr_ids)


def _collect_text_http_signals(text: str) -> _HttpSignals:
    """Собрать HTTP-сигналы из сырого текста через regex."""
    signals = _HttpSignals()

    for match in _CORR_ID_JSON_RE.finditer(text):
        signals.corr_ids.setdefault(match.group("key"), match.group("value"))
    for match in _CORR_ID_KV_RE.finditer(text):
        signals.corr_ids.setdefault(match.group("key"), match.group("value"))
    for match in _CORR_ID_XML_RE.finditer(text):
        signals.corr_ids.setdefault(match.group("key"), match.group("value"))

    for match in _HTTP_STATUS_RE.finditer(text):
        status = match.group(1)
        if status not in signals.http_statuses:
            signals.http_statuses.append(status)

    for match in _STATUS_CODE_RE.finditer(text):
        status = match.group("value")
        if status not in signals.http_statuses:
            signals.http_statuses.append(status)

    for match in _ERROR_FIELD_RE.finditer(text):
        value = match.group("value").strip()
        if value:
            signals.error_fields[match.group("key")] = value

    for match in _XML_ERROR_RE.finditer(text):
        value = match.group("value").strip()
        if value:
            signals.error_fields.setdefault(match.group("key"), value)

    for match in _CONTEXT_FIELD_RE.finditer(text):
        value = match.group("value").strip()
        if value:
            signals.context_fields[match.group("key")] = value

    return signals


def _extract_text_http_info(text: str) -> str:
    """Извлечь HTTP-контекст из сырого текста через regex.

    Возвращает форматированную строку или пустую строку если ничего не найдено.
    """
    return _format_http_info(_collect_text_http_signals(text))


# ---------------------------------------------------------------------------
# JSON scanning for HTTP info
# ---------------------------------------------------------------------------

_JSON_CORR_KEYS = frozenset({"rquid", "operuid", "requestid", "correlationid", "traceid"})
_JSON_STATUS_KEYS = frozenset({"status", "statuscode", "httpstatus", "responsestatus"})
_JSON_ERROR_KEYS = frozenset({
    "error", "errorcode", "errormessage",
    "fault", "faultcode", "faultstring",
    "cause", "reason",
})
_JSON_CONTEXT_KEYS = frozenset({"message", "description", "details"})
_JSON_MAX_DEPTH = 10
_JSON_VALUE_MAX_CHARS = 300


def _collect_json_signals(
    obj: Any,
    corr_ids: dict[str, str],
    http_statuses: list[str],
    error_fields: dict[str, str],
    context_fields: dict[str, str],
    depth: int,
) -> None:
    """Рекурсивно собрать HTTP-сигналы из JSON-объекта."""
    if depth > _JSON_MAX_DEPTH:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in _JSON_CORR_KEYS:
                if isinstance(v, (str, int)) and str(v).strip():
                    corr_ids.setdefault(str(k), str(v)[:64])
            elif kl in _JSON_STATUS_KEYS:
                if isinstance(v, int) and 400 <= v <= 599:
                    status = str(v)
                    if status not in http_statuses:
                        http_statuses.append(status)
            elif kl in _JSON_ERROR_KEYS:
                if isinstance(v, str) and v.strip():
                    error_fields.setdefault(str(k), v[:_JSON_VALUE_MAX_CHARS])
                else:
                    _collect_json_signals(v, corr_ids, http_statuses, error_fields, context_fields, depth + 1)
            elif kl in _JSON_CONTEXT_KEYS:
                if isinstance(v, str) and v.strip():
                    context_fields.setdefault(str(k), v[:_JSON_VALUE_MAX_CHARS])
            else:
                _collect_json_signals(v, corr_ids, http_statuses, error_fields, context_fields, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _collect_json_signals(item, corr_ids, http_statuses, error_fields, context_fields, depth + 1)


def _scan_json_for_http_signals(obj: Any) -> _HttpSignals:
    """Собрать HTTP-сигналы из распарсенного JSON-объекта."""
    corr_ids: dict[str, str] = {}
    http_statuses: list[str] = []
    error_fields: dict[str, str] = {}
    context_fields: dict[str, str] = {}

    _collect_json_signals(obj, corr_ids, http_statuses, error_fields, context_fields, 0)
    return _HttpSignals(
        corr_ids=corr_ids,
        http_statuses=http_statuses,
        error_fields=error_fields,
        context_fields=context_fields,
    )


def _scan_json_for_http_info(obj: Any) -> str:
    """Извлечь HTTP-контекст из распарсенного JSON-объекта."""
    return _format_http_info(_scan_json_for_http_signals(obj))


def _try_parse_json_signals(content: bytes) -> list[_HttpSignals] | None:
    """Потоково распарсить JSON через ijson и извлечь HTTP-сигналы.

    Поддерживает одиночный объект и NDJSON (multiple_values=True).
    Возвращает None при ошибке парсинга (сигнал для regex fallback).
    """
    try:
        signals_list: list[_HttpSignals] = []
        for obj in ijson.items(BytesIO(content), "", multiple_values=True):
            signals_list.append(_scan_json_for_http_signals(obj))
        return signals_list
    except Exception:
        return None


def _extract_http_artifacts(
    content: bytes,
    content_type: str,
    *,
    text: str | None = None,
) -> tuple[str, str | None]:
    """Вернуть ``(http_section, correlation_hint)`` для аттачмента."""
    if content_type == "json":
        parsed = _try_parse_json_signals(content)
        if parsed is not None:
            sections = [
                section
                for signals in parsed
                if (section := _format_http_info(signals))
            ]
            correlation_hint = next(
                (
                    correlation
                    for signals in parsed
                    if (correlation := _format_correlation_hint(signals)) is not None
                ),
                None,
            )
            return "\n\n".join(sections), correlation_hint

    if text is None:
        text = _decode_text(content)
    if text is None:
        return "", None

    signals = _collect_text_http_signals(text)
    return _format_http_info(signals), _format_correlation_hint(signals)


def _detect_and_extract_http(
    content: bytes,
    content_type: str,
    *,
    text: str | None = None,
) -> str:
    """Извлечь HTTP-контекст из аттачмента.

    Для JSON: потоковый парсинг через ijson, при ошибке — regex.
    Для XML/text: regex по декодированному тексту.
    text — уже декодированный текст (передаётся чтобы избежать повторного декодирования).
    """
    http_info, _correlation_hint = _extract_http_artifacts(
        content,
        content_type,
        text=text,
    )
    return http_info


class LogExtractionConfig:
    """Параметры извлечения логов из аттачментов."""

    def __init__(self, *, concurrency: int = 5) -> None:
        self.concurrency = concurrency


_PROCESSABLE_MIME_EXACT = frozenset({
    "application/json",
    "text/json",
    "application/xml",
    "text/xml",
    "application/x-ndjson",
})


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
    """Извлекает ERROR-блоки из текстовых аттачментов и HTTP-контекст из JSON/XML."""

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
        """Скачать аттачменты для каждого теста и заполнить ``log_snippet`` in-place.

        Для каждого аттачмента:
        1. Определить тип содержимого через python-magic.
        2. text/plain: извлечь [ERROR]-блоки + HTTP-контекст через regex.
        3. JSON: потоковый парсинг через ijson, fallback на regex.
        4. XML: regex по тексту.
        5. binary: пропустить.
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

            processable = [
                att for att in all_attachments
                if self._is_processable_attachment(att)
            ]
            if not processable:
                return

            all_sections: list[str] = []
            correlation_hint: str | None = None

            for att in processable:
                if att.id is None:
                    continue
                async with semaphore:
                    try:
                        content_bytes = await self._provider.get_attachment_content(att.id)
                    except Exception as exc:
                        logger.warning(
                            "Логи: не удалось скачать аттачмент %d (%s) для теста %d: %s",
                            att.id,
                            att.name,
                            summary.test_result_id,
                            exc,
                        )
                        continue

                att_name = (att.name or f"attachment-{att.id}").replace("\n", " ").replace("\r", " ").strip()
                fallback_mime = (att.type or att.content_type or "").lower()
                detected_type = _detect_content_type(content_bytes, fallback_mime=fallback_mime)

                if detected_type == "binary":
                    continue

                decoded_text: str | None = None

                # Канал 1: лог-экстрактор (только text)
                if detected_type == "text":
                    decoded_text = _decode_text(content_bytes)
                    if decoded_text:
                        log_blocks = _extract_error_blocks(decoded_text)
                        if log_blocks.strip():
                            all_sections.append(
                                f"--- [файл: {att_name}] ---\n{log_blocks}"
                            )

                # Канал 2: HTTP-экстрактор (все не-binary типы)
                http_info, current_correlation = _extract_http_artifacts(
                    content_bytes,
                    detected_type,
                    text=decoded_text,
                )
                if correlation_hint is None and current_correlation is not None:
                    correlation_hint = current_correlation
                if http_info.strip():
                    all_sections.append(f"--- [HTTP: {att_name}] ---\n{http_info}")

            summary.correlation_hint = correlation_hint
            if all_sections:
                combined = "\n\n".join(all_sections)
                summary.log_snippet = combined

                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Логи: тест %d — секций: %d, общий размер: %d символов",
                        summary.test_result_id,
                        len(all_sections),
                        len(combined),
                    )
            elif logger.isEnabledFor(logging.DEBUG) and correlation_hint is not None:
                logger.debug(
                    "Логи: тест %d — сохранена только correlation hint: %s",
                    summary.test_result_id,
                    correlation_hint,
                )

        tasks = [fetch_and_extract(s) for s in summaries]
        await asyncio.gather(*tasks)

        enriched = sum(1 for s in summaries if s.log_snippet or s.correlation_hint)
        logger.info("Логи: обогащено %d/%d тестов", enriched, len(summaries))

    @staticmethod
    def _is_processable_attachment(att: AttachmentMeta) -> bool:
        """Проверить, может ли аттачмент содержать обрабатываемый контент.

        Первичный MIME-фильтр до скачивания: отсекает очевидные бинарные файлы.
        Точный тип определяется после скачивания через _detect_content_type.
        """
        mime = (att.type or att.content_type or "").lower().split(";")[0].strip()
        if mime in _PROCESSABLE_MIME_EXACT:
            return True
        if mime.startswith("text/"):
            return True
        if not mime:
            return True
        return False
