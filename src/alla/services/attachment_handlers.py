"""Pluggable обработчики содержимого вложений.

Каждый handler решает, может ли он распознать содержимое конкретного
вложения, и возвращает готовую секцию для ``log_snippet`` кластера.
Регистрация — через ``DEFAULT_ATTACHMENT_HANDLERS`` или кастомный список,
переданный в ``LogExtractionService``.

Базовое поведение (text → ERROR-блоки, JSON/XML/text → HTTP-сигналы)
реализовано как два встроенных handler-а, чтобы расширение новыми
типами логов не трогало core-сервис.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Protocol, runtime_checkable

import ijson

from alla.models.testops import AttachmentMeta
from alla.utils.log_utils import format_correlation_pairs

logger = logging.getLogger(__name__)


@dataclass
class AttachmentContext:
    """Окружение, в котором handler принимает решение о вложении."""

    att: AttachmentMeta
    content: bytes
    detected_type: str
    decoded_text: str | None = None


@dataclass
class HandlerResult:
    """Результат работы handler-а для одного вложения."""

    section: str
    label: str
    correlation_hint: str | None = None
    consumed: bool = True


@runtime_checkable
class AttachmentHandler(Protocol):
    name: str
    priority: int

    def handle(self, ctx: AttachmentContext) -> HandlerResult | None: ...


# ---------------------------------------------------------------------------
# StructuredErrorLogHandler
# ---------------------------------------------------------------------------

# Сигнатурные ключи структурированной лог-записи. Если в большинстве
# объектов JSON-массива встречается ≥2 ключа из этого набора — значит,
# перед нами журнал ошибок, а не случайный JSON-массив.
_STRUCTURED_LOG_SIGNATURE_KEYS = frozenset({
    "message",
    "loglevel",
    "stacktrace",
    "subsystem",
    "errorcode",
    "timestamp",
})

# Ключи для извлечения correlation-IDs (lowercased).
_STRUCTURED_LOG_CORR_KEYS = frozenset({
    "rquid",
    "operuid",
    "requestid",
    "correlationid",
    "traceid",
})

# Минимальная доля объектов, которые должны выглядеть как лог-запись.
_STRUCTURED_LOG_MATCH_RATIO = 0.6
# Минимальное число ключей-сигнатуры в одном объекте, чтобы он считался "лог-записью".
_STRUCTURED_LOG_MIN_KEYS = 2


def _parse_json_array(content: bytes, decoded_text: str | None) -> list[Any] | None:
    """Распарсить тело как JSON-массив. Возвращает None, если не массив."""
    if decoded_text is not None:
        try:
            obj = json.loads(decoded_text)
        except (ValueError, json.JSONDecodeError):
            obj = None
        if isinstance(obj, list):
            return obj
        if obj is not None:
            return None  # это валидный JSON, но не массив

    # Fallback на потоковый парсер для случаев, когда decoded_text был None
    # (decode не справился) — пробуем напрямую по байтам.
    try:
        items = list(ijson.items(BytesIO(content), "item"))
    except Exception:
        return None
    return items if items else None


def _looks_like_structured_log(items: list[Any]) -> bool:
    """Эвристика: похож ли список на журнал структурированных лог-записей."""
    if not items:
        return False

    matched = 0
    for entry in items:
        if not isinstance(entry, dict):
            continue
        keys_lower = {str(k).lower() for k in entry}
        overlap = len(keys_lower & _STRUCTURED_LOG_SIGNATURE_KEYS)
        if overlap >= _STRUCTURED_LOG_MIN_KEYS:
            matched += 1

    return matched / len(items) >= _STRUCTURED_LOG_MATCH_RATIO


def _format_structured_entry(index: int, entry: dict[str, Any]) -> str:
    """Сформатировать одну лог-запись как читаемый блок."""
    # Приводим ключи к нижнему регистру один раз для приоритизации.
    entry_lower = {str(k).lower(): k for k in entry}

    header_parts: list[str] = [f"[#{index}]"]
    log_level_key = entry_lower.get("loglevel")
    if log_level_key is not None:
        header_parts.append(f"logLevel={entry[log_level_key]}")
    subsystem_key = entry_lower.get("subsystem")
    if subsystem_key is not None:
        header_parts.append(f"subsystem={entry[subsystem_key]}")
    timestamp_key = entry_lower.get("timestamp")
    if timestamp_key is not None:
        header_parts.append(f"timestamp={entry[timestamp_key]}")

    lines: list[str] = ["  ".join(header_parts)]

    # message и errorCode — в первую очередь
    for priority_key in ("message", "errorcode"):
        original = entry_lower.get(priority_key)
        if original is None:
            continue
        value = entry[original]
        if value is None or value == "":
            continue
        lines.append(f"{original}: {value}")

    # stackTrace — отдельным блоком с переносом
    stacktrace_key = entry_lower.get("stacktrace")
    if stacktrace_key is not None:
        value = entry[stacktrace_key]
        if value:
            lines.append(f"{stacktrace_key}:")
            for st_line in str(value).splitlines() or [str(value)]:
                lines.append(f"  {st_line}")

    # Остальные поля — одной строкой
    handled = {"loglevel", "subsystem", "timestamp", "message", "errorcode", "stacktrace"}
    for key_lower, original in entry_lower.items():
        if key_lower in handled:
            continue
        value = entry[original]
        if value is None or value == "":
            continue
        # Скаляры — в одну строку, словари/списки — через json.dumps.
        if isinstance(value, (dict, list)):
            try:
                serialized = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                serialized = str(value)
            lines.append(f"{original}: {serialized}")
        else:
            lines.append(f"{original}: {value}")

    return "\n".join(lines)


def _extract_structured_correlation(items: list[Any]) -> str | None:
    """Найти первый объект с correlation-полями и сформировать строку."""
    for entry in items:
        if not isinstance(entry, dict):
            continue
        pairs: dict[str, str] = {}
        for raw_key, raw_value in entry.items():
            if str(raw_key).lower() not in _STRUCTURED_LOG_CORR_KEYS:
                continue
            if isinstance(raw_value, (str, int)) and str(raw_value).strip():
                pairs.setdefault(str(raw_key), str(raw_value))
        formatted = format_correlation_pairs(pairs)
        if formatted is not None:
            return formatted
    return None


@dataclass
class StructuredErrorLogHandler:
    """Распознаёт JSON-массив структурированных лог-записей."""

    name: str = "structured-error-log"
    priority: int = 10
    label: str = "журнал"

    def handle(self, ctx: AttachmentContext) -> HandlerResult | None:
        if ctx.detected_type not in {"json", "text"}:
            return None

        items = _parse_json_array(ctx.content, ctx.decoded_text)
        if items is None:
            return None

        if not _looks_like_structured_log(items):
            return None

        formatted_blocks: list[str] = []
        for idx, entry in enumerate(items, start=1):
            if not isinstance(entry, dict):
                continue
            formatted_blocks.append(_format_structured_entry(idx, entry))

        if not formatted_blocks:
            return None

        section = "\n---\n".join(formatted_blocks)
        correlation_hint = _extract_structured_correlation(items)
        return HandlerResult(
            section=section,
            label=self.label,
            correlation_hint=correlation_hint,
            consumed=True,
        )


# ---------------------------------------------------------------------------
# ErrorBlocksHandler — обёртка над _extract_error_blocks для text
# ---------------------------------------------------------------------------


@dataclass
class ErrorBlocksHandler:
    """Извлекает [ERROR] блоки из текстовых аттачментов."""

    name: str = "error-blocks"
    priority: int = 50
    label: str = "файл"

    def handle(self, ctx: AttachmentContext) -> HandlerResult | None:
        if ctx.detected_type != "text":
            return None
        if ctx.decoded_text is None:
            return None
        # Импорт внутри метода чтобы избежать циклической зависимости
        # (log_extraction_service импортирует attachment_handlers).
        from alla.services.log_extraction_service import _extract_error_blocks

        blocks = _extract_error_blocks(ctx.decoded_text)
        if not blocks.strip():
            return None
        return HandlerResult(
            section=blocks,
            label=self.label,
            correlation_hint=None,
            consumed=False,
        )


# ---------------------------------------------------------------------------
# HttpSignalsHandler — обёртка над _extract_http_artifacts
# ---------------------------------------------------------------------------


@dataclass
class HttpSignalsHandler:
    """Извлекает HTTP-сигналы (correlation/status/error) из любого текстового вложения."""

    name: str = "http-signals"
    priority: int = 60
    label: str = "HTTP"

    def handle(self, ctx: AttachmentContext) -> HandlerResult | None:
        if ctx.detected_type == "binary":
            return None
        from alla.services.log_extraction_service import _extract_http_artifacts

        http_info, correlation_hint = _extract_http_artifacts(
            ctx.content,
            ctx.detected_type,
            text=ctx.decoded_text,
        )
        if not http_info.strip():
            # Корреляция могла остаться даже если HTTP-секция пустая —
            # сервис её подхватит из correlation_hint всё равно.
            if correlation_hint is None:
                return None
            return HandlerResult(
                section="",
                label=self.label,
                correlation_hint=correlation_hint,
                consumed=False,
            )
        return HandlerResult(
            section=http_info,
            label=self.label,
            correlation_hint=correlation_hint,
            consumed=False,
        )


# ---------------------------------------------------------------------------
# Реестр по умолчанию
# ---------------------------------------------------------------------------


def default_handlers() -> list[AttachmentHandler]:
    """Default-список обработчиков, упорядоченный по priority."""
    handlers: list[AttachmentHandler] = [
        StructuredErrorLogHandler(),
        ErrorBlocksHandler(),
        HttpSignalsHandler(),
    ]
    handlers.sort(key=lambda h: h.priority)
    return handlers
