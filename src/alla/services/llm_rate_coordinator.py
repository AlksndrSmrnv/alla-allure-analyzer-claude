"""Глобальный координатор обращений к LLM.

При нескольких параллельных анализах разных launch'ей все запросы к GigaChat
проходят через один process-wide координатор. Это даёт общий потолок RPS,
единый 429-кулдаун (когда любой запрос ловит 429, все остальные тормозят),
честную FIFO-очередь между кластерами разных launch'ей и jitter, чтобы
запросы не выходили из кулдауна одновременно.
"""

from __future__ import annotations

import asyncio
import logging
import random
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:
    from alla.config import Settings

logger = logging.getLogger(__name__)


_JITTER_RATIO = 0.2
_COOLDOWN_FLOOR = 5.0
_COOLDOWN_CAP = 120.0


class LLMRateCoordinator:
    """Process-wide координатор: семафор + RPS-gate + 429-кулдаун."""

    def __init__(
        self,
        *,
        concurrency: int = 3,
        min_interval: float = 2.0,
    ) -> None:
        self._semaphore = asyncio.Semaphore(concurrency)
        self._concurrency = concurrency
        self._min_interval = max(0.0, min_interval)
        self._rps_lock = asyncio.Lock()
        self._last_dispatch: float = 0.0
        self._cooldown_lock = asyncio.Lock()
        self._cooldown_until: float = 0.0
        self._in_flight = 0
        self._cooldown_count = 0

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        """Зарезервировать слот для одного LLM-запроса.

        Порядок:
        1. ждём окончания глобального 429-кулдауна;
        2. занимаем слот семафора (FIFO);
        3. ещё раз проверяем кулдаун (его могли поставить, пока стояли в очереди);
        4. под rps-lock выдерживаем ``min_interval + jitter`` от прошлого dispatch;
        5. yield — клиент делает запрос;
        6. освобождаем слот семафора (cooldown НЕ сбрасываем).
        """
        await self.wait_cooldown()
        await self._semaphore.acquire()
        try:
            await self.wait_cooldown()
            await self._wait_rps_gate()
            self._in_flight += 1
            try:
                yield
            finally:
                self._in_flight -= 1
        finally:
            self._semaphore.release()

    async def wait_cooldown(self) -> None:
        """Подождать, пока истечёт глобальный 429-кулдаун."""
        async with self._cooldown_lock:
            remaining = self._cooldown_until - asyncio.get_running_loop().time()
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def trigger_cooldown(self, delay: float, *, reason: str) -> None:
        """Установить/продлить глобальный кулдаун.

        Берётся максимум из текущего и нового значения (не суммируется).
        Применяется floor (минимум при пустом ``Retry-After``) и cap.
        """
        clamped = min(_COOLDOWN_CAP, max(_COOLDOWN_FLOOR, delay))
        async with self._cooldown_lock:
            new_until = asyncio.get_running_loop().time() + clamped
            if new_until > self._cooldown_until:
                self._cooldown_until = new_until
                self._cooldown_count += 1
                logger.warning(
                    "LLM rate-coordinator: глобальный кулдаун %.1fs (причина: %s)",
                    clamped,
                    reason,
                )

    def jitter_backoff(self, base_delay: float) -> float:
        """Equal jitter: ``base/2 + uniform(0, base/2)``.

        Снимает thundering herd при выходе нескольких задач из общего кулдауна.
        """
        if base_delay <= 0:
            return 0.0
        half = base_delay / 2
        return half + random.uniform(0.0, half)

    def jitter_retry_after(self, server_delay: float) -> float:
        """Добавить небольшой jitter сверху Retry-After (до +20%)."""
        if server_delay <= 0:
            return 0.0
        return server_delay + random.uniform(0.0, server_delay * _JITTER_RATIO)

    def stats(self) -> dict[str, float | int]:
        """Текущее состояние — для тестов и диагностики."""
        loop = asyncio.get_event_loop()
        cooldown_remaining = max(0.0, self._cooldown_until - loop.time())
        return {
            "concurrency": self._concurrency,
            "min_interval": self._min_interval,
            "in_flight": self._in_flight,
            "cooldown_remaining": cooldown_remaining,
            "cooldown_count": self._cooldown_count,
        }

    async def _wait_rps_gate(self) -> None:
        """Выдержать ``min_interval + jitter`` от предыдущего dispatch."""
        if self._min_interval <= 0:
            return
        async with self._rps_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            target_interval = self._min_interval * random.uniform(
                1.0 - _JITTER_RATIO,
                1.0 + _JITTER_RATIO,
            )
            elapsed = now - self._last_dispatch
            if self._last_dispatch > 0 and elapsed < target_interval:
                await asyncio.sleep(target_interval - elapsed)
            self._last_dispatch = loop.time()


_instance: LLMRateCoordinator | None = None


def get_coordinator(settings: "Settings | None" = None) -> LLMRateCoordinator:
    """Вернуть process-wide координатор, создав его при первом вызове.

    ``settings`` используется только при первой инициализации; повторные
    вызовы возвращают тот же инстанс. Чтобы пересоздать координатор с новыми
    настройками, вызовите :func:`reset_coordinator`.
    """
    global _instance
    if _instance is None:
        if settings is None:
            _instance = LLMRateCoordinator()
        else:
            _instance = LLMRateCoordinator(
                concurrency=settings.llm_concurrency,
                min_interval=settings.llm_request_delay,
            )
    return _instance


def reset_coordinator() -> None:
    """Сбросить singleton (для тестов и server lifespan startup)."""
    global _instance
    _instance = None
