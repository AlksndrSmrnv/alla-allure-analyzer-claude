"""Сервис извлечения ERROR-блоков из текстовых аттачментов.

Скачивает text/plain аттачменты для каждого упавшего теста, извлекает строки
с уровнем [ERROR] и их stack trace, помечает каждый блок именем файла-источника
и сохраняет результат в ``FailedTestSummary.log_snippet``.
"""

import asyncio
import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

import ijson

from alla.clients.base import AttachmentProvider
from alla.models.testops import AttachmentMeta, FailedTestSummary
from alla.services.attachment_handlers import (
    AttachmentContext,
    AttachmentHandler,
    default_handlers,
)
from alla.utils.log_utils import (
    extract_correlation_pairs_from_json,
    extract_correlation_pairs_from_text,
    format_correlation_pairs,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Определение content type и декодирование текста
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

    for key, value in extract_correlation_pairs_from_text(text).items():
        signals.corr_ids.setdefault(key, value)

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
# Сканирование JSON для HTTP-информации
# ---------------------------------------------------------------------------

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
            if kl in _JSON_STATUS_KEYS:
                if isinstance(v, int) and 400 <= v <= 599:
                    status = str(v)
                    if status not in http_statuses:
                        http_statuses.append(status)
            elif kl in _JSON_ERROR_KEYS:
                if isinstance(v, str) and v.strip():
                    error_fields.setdefault(str(k), v[:_JSON_VALUE_MAX_CHARS])
                else:
                    _collect_json_signals(
                        v,
                        http_statuses,
                        error_fields,
                        context_fields,
                        depth + 1,
                    )
            elif kl in _JSON_CONTEXT_KEYS:
                if isinstance(v, str) and v.strip():
                    context_fields.setdefault(str(k), v[:_JSON_VALUE_MAX_CHARS])
            else:
                _collect_json_signals(
                    v,
                    http_statuses,
                    error_fields,
                    context_fields,
                    depth + 1,
                )
    elif isinstance(obj, list):
        for item in obj:
            _collect_json_signals(
                item,
                http_statuses,
                error_fields,
                context_fields,
                depth + 1,
            )


def _scan_json_for_http_signals(obj: Any) -> _HttpSignals:
    """Собрать HTTP-сигналы из распарсенного JSON-объекта."""
    corr_ids = extract_correlation_pairs_from_json(obj, max_depth=_JSON_MAX_DEPTH)
    http_statuses: list[str] = []
    error_fields: dict[str, str] = {}
    context_fields: dict[str, str] = {}

    _collect_json_signals(obj, http_statuses, error_fields, context_fields, 0)
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


def _extract_status_details_correlation_hint(
    summary: FailedTestSummary,
) -> str | None:
    """Извлечь correlation hint из status_message/status_trace теста."""
    trace_text = "\n".join(
        part for part in (summary.status_message, summary.status_trace) if part
    )
    if not trace_text:
        return None
    return format_correlation_pairs(extract_correlation_pairs_from_text(trace_text))


def _apply_status_details_correlation_fallback(
    summary: FailedTestSummary,
    correlation_hint: str | None,
) -> str | None:
    """Вернуть attachment correlation или fallback из statusDetails."""
    if correlation_hint is not None:
        return correlation_hint

    fallback = _extract_status_details_correlation_hint(summary)
    if fallback is not None and logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "Логи: тест %d — correlation_hint извлечён из statusDetails: %s",
            summary.test_result_id,
            fallback,
        )
    return fallback


class LogExtractionConfig:
    """Параметры извлечения логов из аттачментов."""

    def __init__(
        self,
        *,
        concurrency: int = 5,
        max_snippet_chars: int = 64 * 1024,
    ) -> None:
        self.concurrency = concurrency
        self.max_snippet_chars = max_snippet_chars


_PROCESSABLE_MIME_EXACT = frozenset({
    "application/json",
    "text/json",
    "application/xml",
    "text/xml",
    "application/x-ndjson",
})
_DETAILS_MAX_APPEND_CHARS = 4096
_DETAILS_ALLOWED_EXTENSIONS = frozenset({"", ".txt", ".log"})


def _is_details_attachment(att: AttachmentMeta) -> bool:
    """Проверить, похоже ли вложение на TestOps Details с Expected/Actual."""
    raw_name = (att.name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    stem, ext = os.path.splitext(raw_name)
    if stem.lower() != "details":
        return False
    if ext.lower() not in _DETAILS_ALLOWED_EXTENSIONS:
        return False

    mime = (att.type or att.content_type or "").lower().split(";")[0].strip()
    return not mime or mime == "text/plain" or mime.startswith("text/")


def _augment_status_message_with_details(
    summary: FailedTestSummary,
    details_text: str,
) -> bool:
    """Добавить Details-текст в status_message с нормализацией и cap."""
    normalized = details_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return False

    current = summary.status_message or ""
    current_normalized = current.replace("\r\n", "\n").replace("\r", "\n")
    if normalized in current or normalized in current_normalized:
        return False

    if len(normalized) > _DETAILS_MAX_APPEND_CHARS:
        normalized = (
            normalized[:_DETAILS_MAX_APPEND_CHARS]
            + "\n... [Details обрезаны]"
        )

    summary.status_message = normalized if not current else f"{current}\n\n{normalized}"
    return True


# ---------------------------------------------------------------------------
# Извлечение ERROR-блоков
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
                # Продолжение (stack trace) или ещё один [ERROR] без timestamp.
                current_block.append(line)

    # Финальный блок
    if current_block:
        blocks.append("\n".join(current_block))

    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Сервис LogExtractionService
# ---------------------------------------------------------------------------

class LogExtractionService:
    """Извлекает структурированные секции из вложений через цепочку handlers.

    Default-набор обработчиков покрывает старое поведение (ERROR-блоки в text,
    HTTP-сигналы в JSON/XML/text). Новые типы логов добавляются через кастомный
    список ``handlers`` без правок самого сервиса.
    """

    def __init__(
        self,
        provider: AttachmentProvider,
        config: LogExtractionConfig | None = None,
        *,
        handlers: Sequence[AttachmentHandler] | None = None,
    ) -> None:
        self._provider = provider
        self._config = config or LogExtractionConfig()
        # Сортируем handlers по priority один раз в конструкторе.
        chosen = list(handlers) if handlers is not None else default_handlers()
        chosen.sort(key=lambda h: h.priority)
        self._handlers: list[AttachmentHandler] = chosen

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

            details_atts = [
                att for att in all_attachments
                if _is_details_attachment(att)
            ]
            if details_atts:
                details_att = details_atts[0]
                if details_att.id is None:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "Details: тест %d — вложение %s без id, пропуск",
                            summary.test_result_id,
                            details_att.name,
                        )
                else:
                    async with semaphore:
                        try:
                            details_bytes = await self._provider.get_attachment_content(
                                details_att.id,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Details: не удалось скачать аттачмент %d (%s) "
                                "для теста %d: %s",
                                details_att.id,
                                details_att.name,
                                summary.test_result_id,
                                exc,
                            )
                            details_bytes = b""

                    if details_bytes:
                        decoded_details = _decode_text(details_bytes)
                        if decoded_details is None:
                            if logger.isEnabledFor(logging.DEBUG):
                                logger.debug(
                                    "Details: тест %d — аттачмент %d (%s) "
                                    "не декодирован как текст",
                                    summary.test_result_id,
                                    details_att.id,
                                    details_att.name,
                                )
                        else:
                            before_len = len(summary.status_message or "")
                            if _augment_status_message_with_details(
                                summary,
                                decoded_details,
                            ):
                                added_len = len(summary.status_message or "") - before_len
                                logger.info(
                                    "Details: тест %d — добавлено %d символов "
                                    "в status_message",
                                    summary.test_result_id,
                                    added_len,
                                )
                            elif logger.isEnabledFor(logging.DEBUG):
                                logger.debug(
                                    "Details: тест %d — аттачмент %d (%s) "
                                    "пустой или уже присутствует в status_message",
                                    summary.test_result_id,
                                    details_att.id,
                                    details_att.name,
                                )

            processable = [
                att for att in all_attachments
                if att not in details_atts and self._is_processable_attachment(att)
            ]
            if not processable:
                summary.correlation_hint = _apply_status_details_correlation_fallback(
                    summary,
                    None,
                )
                return

            # Бюджетируем log_snippet ПО МЕРЕ накопления, а не после join:
            # full combined в памяти не строится, лишние секции не удерживаются.
            max_chars = self._config.max_snippet_chars
            budget = max_chars if max_chars and max_chars > 0 else None
            kept_sections: list[str] = []
            kept_total = 0  # уже занятый бюджет (с учётом разделителей "\n\n")
            original_total = 0  # сколько символов было бы без обрезки
            truncated = False
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

                # Декодируем текст один раз для тех handlers, которым он нужен.
                # JSON/XML/text — все с большой вероятностью текст; декод дешёвый
                # и кэшируется в context, чтобы handlers не делали это повторно.
                decoded_text = _decode_text(content_bytes)

                ctx = AttachmentContext(
                    att=att,
                    content=content_bytes,
                    detected_type=detected_type,
                    decoded_text=decoded_text,
                )

                try:
                    for handler in self._handlers:
                        try:
                            result = handler.handle(ctx)
                        except Exception as exc:
                            logger.warning(
                                "Логи: handler %s упал на аттачменте %d (%s) теста %d: %s",
                                handler.name,
                                att.id,
                                att.name,
                                summary.test_result_id,
                                exc,
                            )
                            continue
                        if result is None:
                            continue
                        if result.correlation_hint and correlation_hint is None:
                            correlation_hint = result.correlation_hint
                        if result.section.strip():
                            section_text = (
                                f"--- [{result.label}: {att_name}] ---\n{result.section}"
                            )
                            sep_len = 2 if kept_sections else 0  # "\n\n"
                            original_total += sep_len + len(section_text)

                            if budget is None:
                                kept_sections.append(section_text)
                            elif not truncated:
                                if sep_len + len(section_text) <= budget - kept_total:
                                    kept_sections.append(section_text)
                                    kept_total += sep_len + len(section_text)
                                else:
                                    # Помещается только часть секции — режем её,
                                    # ставим флаг и больше ничего не копим.
                                    available = budget - kept_total - sep_len
                                    if available > 0:
                                        kept_sections.append(section_text[:available])
                                        kept_total += sep_len + available
                                    truncated = True
                            # else: bucket исчерпан — original_total продолжаем
                            # считать, но в память больше ничего не складываем.
                            section_text = ""  # noqa: F841 — освобождаем ссылку
                        if result.consumed:
                            break
                finally:
                    # Освобождаем сырые байты и декодированный текст сразу после
                    # обработки handler-ами, чтобы не держать их в памяти до
                    # следующей итерации (важно при большом числе/размере
                    # аттачментов под памятным лимитом).
                    ctx.content = b""
                    ctx.decoded_text = None
                    content_bytes = b""
                    decoded_text = None

            correlation_hint = _apply_status_details_correlation_fallback(
                summary,
                correlation_hint,
            )
            summary.correlation_hint = correlation_hint
            if kept_sections:
                combined = "\n\n".join(kept_sections)
                if truncated:
                    combined += (
                        f"\n\n[... обрезано: было {original_total} символов, "
                        f"оставлено {max_chars} ...]"
                    )
                    logger.debug(
                        "Логи: тест %d — log_snippet обрезан до %d символов "
                        "(полный размер был бы %d)",
                        summary.test_result_id,
                        max_chars,
                        original_total,
                    )
                summary.log_snippet = combined

                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Логи: тест %d — секций: %d, общий размер: %d символов",
                        summary.test_result_id,
                        len(kept_sections),
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
