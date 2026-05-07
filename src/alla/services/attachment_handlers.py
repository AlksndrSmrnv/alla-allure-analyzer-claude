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

# =====================================================================
# КОНФИГУРАЦИЯ РАСПОЗНАВАНИЯ ЖУРНАЛА — РЕДАКТИРУЙТЕ ЗДЕСЬ
# =====================================================================
# Чтобы handler признал JSON-массив структурированным журналом ошибок,
# в каждой записи (точнее — в большинстве записей, см.
# ``_STRUCTURED_LOG_MATCH_RATIO``) должны присутствовать ВСЕ ключи из
# набора ниже. Сравнение регистронезависимое — указывайте ключи в
# lower-case.
#
# Чтобы добавить или убрать обязательные поля сигнатуры — отредактируйте
# этот frozenset. Больше ничего менять не нужно: алгоритм автоматически
# подхватит новый набор.
_STRUCTURED_LOG_REQUIRED_KEYS: frozenset[str] = frozenset({
    "deploymentunit",
    "tenantcode",
})

# Минимальная доля объектов массива, в которых должны присутствовать ВСЕ
# обязательные ключи, чтобы файл был признан структурированным журналом.
_STRUCTURED_LOG_MATCH_RATIO = 0.6
# =====================================================================

# Ключи для извлечения correlation-IDs (lowercased).
_STRUCTURED_LOG_CORR_KEYS = frozenset({
    "rquid",
    "operuid",
    "requestid",
    "correlationid",
    "traceid",
})


# Максимальная глубина обхода JSON при поиске вложенного массива-журнала.
_STRUCTURED_LOG_SEARCH_MAX_DEPTH = 10


def _parse_any_json(content: bytes, decoded_text: str | None) -> Any:
    """Распарсить тело как любой JSON (dict / list / scalar). None — не JSON."""
    if decoded_text is not None:
        try:
            return json.loads(decoded_text)
        except (ValueError, json.JSONDecodeError):
            pass
    # Fallback: ijson.items с пустым префиксом отдаёт значение целиком.
    try:
        for obj in ijson.items(BytesIO(content), "", multiple_values=True):
            return obj
    except Exception:
        return None
    return None


def _iter_candidate_arrays(obj: Any, depth: int = 0):
    """Обойти JSON и вернуть все list-узлы (включая корневой).

    Используется для поиска массива-журнала, который может лежать как на
    верхнем уровне, так и быть вложенным внутрь произвольных объектов
    (например ``{"data": [...]}`` или ``{"response": {"items": [...]}}``).
    """
    if depth > _STRUCTURED_LOG_SEARCH_MAX_DEPTH:
        return
    if isinstance(obj, list):
        yield obj
        for item in obj:
            yield from _iter_candidate_arrays(item, depth + 1)
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _iter_candidate_arrays(value, depth + 1)


def _find_journal_array(content: bytes, decoded_text: str | None) -> list[Any] | None:
    """Найти первый JSON-массив (на любой глубине), похожий на журнал."""
    parsed = _parse_any_json(content, decoded_text)
    if parsed is None:
        return None
    for candidate in _iter_candidate_arrays(parsed):
        if _looks_like_structured_log(candidate):
            return candidate
    return None


def _looks_like_structured_log(items: list[Any]) -> bool:
    """Эвристика: похож ли список на журнал структурированных лог-записей.

    Объект считается лог-записью, если содержит **все** ключи из
    ``_STRUCTURED_LOG_REQUIRED_KEYS``. Файл признаётся журналом, если
    таких объектов хотя бы ``_STRUCTURED_LOG_MATCH_RATIO`` от общего числа.
    """
    if not items:
        return False
    if not _STRUCTURED_LOG_REQUIRED_KEYS:
        # Пустая сигнатура запретит детект — на всякий случай.
        return False

    matched = 0
    for entry in items:
        if not isinstance(entry, dict):
            continue
        keys_lower = {str(k).lower() for k in entry}
        if keys_lower.issuperset(_STRUCTURED_LOG_REQUIRED_KEYS):
            matched += 1

    return matched / len(items) >= _STRUCTURED_LOG_MATCH_RATIO


_STRUCTURED_LOG_CORR_MAX_DEPTH = 10


def _collect_corr_ids_recursive(
    obj: Any,
    pairs: dict[str, str],
    depth: int = 0,
) -> None:
    """Рекурсивно собрать correlation-IDs из вложенных dict/list."""
    if depth > _STRUCTURED_LOG_CORR_MAX_DEPTH:
        return
    if isinstance(obj, dict):
        for raw_key, raw_value in obj.items():
            if str(raw_key).lower() in _STRUCTURED_LOG_CORR_KEYS:
                if isinstance(raw_value, (str, int)) and str(raw_value).strip():
                    pairs.setdefault(str(raw_key), str(raw_value))
                    continue
            _collect_corr_ids_recursive(raw_value, pairs, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _collect_corr_ids_recursive(item, pairs, depth + 1)


def _extract_structured_correlation(items: list[Any]) -> str | None:
    """Найти первый объект с correlation-полями (любой вложенности).

    Соответствует поведению `_scan_json_for_http_signals` в общем сканере:
    correlation IDs ищутся рекурсивно во всех вложенных dict/list.
    """
    for entry in items:
        if not isinstance(entry, dict):
            continue
        pairs: dict[str, str] = {}
        _collect_corr_ids_recursive(entry, pairs)
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

        items = _find_journal_array(ctx.content, ctx.decoded_text)
        if items is None:
            return None

        # Содержимое отдаём как pretty-printed JSON: структура и порядок полей
        # сохраняются как в исходном файле, читать и парсить (в т.ч. LLM)
        # такой формат проще, чем плоские key/value.
        try:
            section = json.dumps(items, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            # Фолбэк на исходный текст, если в JSON попал не-сериализуемый объект.
            section = ctx.decoded_text or ""
        if not section.strip():
            return None

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
