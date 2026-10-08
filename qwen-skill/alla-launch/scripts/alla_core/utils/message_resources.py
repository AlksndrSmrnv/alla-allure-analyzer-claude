"""Ресурсы в сообщении об ошибке: хост сетевой ошибки и локатор элемента UI.

Одно отличающееся слово в длинном сообщении почти не снижает TF-IDF похожесть, хотя
``Failed to connect to inventory.svc`` и ``… loyalty.svc`` — отказы разных сервисов, а
таймаут ожидания ``#promo-banner`` и ``#gift-wrap-toggle`` — разные элементы. Кластеризация
считает такие сообщения разными (gate по ресурсам), если ресурсы одного вида у двух падений
не пересекаются.

Ресурсы берутся только из известного контекста, а не из любого слова с точкой:
``ru.company.X`` и ``Foo.java:40`` — не хосты, значения в кавычках (тестовые данные) — не
ресурсы. Список шаблонов расширяется, только если эталон это показывает.

* Хост — у отказа соединения (``Connection refused: h:p``, ``connect to h``,
  ``ECONNREFUSED h:p``, ``No route to host: h``) и в URL ``http(s)://h``. IP — ``<ip>``,
  цифры имени — ``#`` (``orders-1`` и ``orders-2`` — реплики одного сервиса), порт
  сохраняется (``localhost:8080`` и ``localhost:5432`` — разные сервисы).
* Ошибка разрешения имени (``UnknownHostException``, ``Could not resolve host``,
  ``ENOTFOUND``…) хостов не даёт: отказывает обычно общий резолвер, а не сервис хоста, и
  сбой DNS бьёт по всем хостам сразу.
* Локатор — Selenide ``Element not found {…}``, Selenium ``Unable to locate element: {…}`` и
  ``By.xxx: …``, Playwright ``waiting for locator(…)``/``getByXxx(…)``, Cypress
  ``Expected to find element: `…` ``, Puppeteer ``waiting for selector "…"``. Пробелы
  схлопнуты, цифры — ``#`` (``:nth-child(2)`` и ``(3)`` — один список).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HOST = (r"(?P<host>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9-]+)*)"
         r"(?::(?P<port>\d{1,5}))?")
_HOST_RES = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"Connection refused(?: \(Connection refused\))?:\s*" + _HOST,
    r"\bconnect to\s+" + _HOST,
    r"\bECONNREFUSED\s+" + _HOST,
    r"No route to host:\s*" + _HOST,
    r"\bhttps?://" + _HOST,
))
_NAME_RESOLUTION_RE = re.compile(
    r"UnknownHostException|Could not resolve host|\bENOTFOUND\b|\bEAI_AGAIN\b|Failed to resolve"
    r"|Name or service not known|nodename nor servname|Temporary failure in name resolution"
    r"|No such host is known",
    re.IGNORECASE,
)
_LOCATOR_RES = tuple(re.compile(pattern, re.MULTILINE) for pattern in (
    r"waiting for ((?:locator|getBy\w+)\(.*\))\s*$",
    r"Element (?:not found|should [^{\n]*) \{([^}\n]+)\}",
    r"Unable to locate element: (\{.*\})",
    r"\bBy\.\w+: ([^\n]+)",
    r"Expected to find element: `([^`]+)`",
    r"(?i:waiting for selector) [`\"'](.+?)[`\"']",
))
_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_DIGITS_RE = re.compile(r"\d+")


@dataclass(frozen=True)
class MessageResources:
    """Хосты и локаторы сообщения в сравнимом виде."""

    hosts: frozenset[str] = frozenset()
    locators: frozenset[str] = frozenset()

    def differ(self, other: MessageResources) -> bool:
        """Ресурсы одного вида есть у обоих и не пересекаются — сообщения о разном."""
        return any(
            mine and theirs and not mine & theirs
            for mine, theirs in ((self.hosts, other.hosts), (self.locators, other.locators))
        )


def _host(match: re.Match[str]) -> str:
    name = match.group("host").casefold()
    name = "<ip>" if _IP_RE.match(name) else _DIGITS_RE.sub("#", name)
    port = match.group("port")
    return f"{name}:{port}" if port else name


def message_resources(message: str | None) -> MessageResources:
    """Хосты и локаторы из сообщения об ошибке; нет сообщения — пусто."""
    if not message:
        return MessageResources()
    hosts: set[str] = set()
    if not _NAME_RESOLUTION_RE.search(message):
        hosts = {_host(match) for pattern in _HOST_RES for match in pattern.finditer(message)}
    locators = {
        _DIGITS_RE.sub("#", " ".join(match.group(1).split()).casefold())
        for pattern in _LOCATOR_RES for match in pattern.finditer(message)
    }
    return MessageResources(frozenset(hosts), frozenset(locators))
