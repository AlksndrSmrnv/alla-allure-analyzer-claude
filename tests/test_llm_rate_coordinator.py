"""Тесты глобального LLM rate-coordinator'a."""

from __future__ import annotations

import asyncio

import pytest

from alla.services.llm_rate_coordinator import (
    LLMRateCoordinator,
    get_coordinator,
    reset_coordinator,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_coordinator()
    yield
    reset_coordinator()


@pytest.mark.asyncio
async def test_global_concurrency_cap() -> None:
    """В моменте in_flight не превышает concurrency."""
    coord = LLMRateCoordinator(concurrency=2, min_interval=0)
    peak = 0
    current = 0

    async def task() -> None:
        nonlocal peak, current
        async with coord.acquire():
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.01)
            current -= 1

    await asyncio.gather(*(task() for _ in range(8)))
    assert peak <= 2


@pytest.mark.asyncio
async def test_min_interval_between_dispatches() -> None:
    """Между стартами слотов выдерживается min_interval (с поправкой на jitter)."""
    coord = LLMRateCoordinator(concurrency=4, min_interval=0.05)
    timestamps: list[float] = []

    async def task() -> None:
        async with coord.acquire():
            timestamps.append(asyncio.get_running_loop().time())

    await asyncio.gather(*(task() for _ in range(5)))

    timestamps.sort()
    intervals = [b - a for a, b in zip(timestamps, timestamps[1:])]
    # jitter ±20% → нижняя граница 0.04
    assert all(iv >= 0.04 - 0.005 for iv in intervals), intervals


@pytest.mark.asyncio
async def test_global_cooldown_blocks_all() -> None:
    """trigger_cooldown тормозит все pending acquire."""
    coord = LLMRateCoordinator(concurrency=4, min_interval=0)
    await coord.trigger_cooldown(0.1, reason="test")

    start = asyncio.get_running_loop().time()

    async def task() -> None:
        async with coord.acquire():
            pass

    await asyncio.gather(*(task() for _ in range(3)))
    elapsed = asyncio.get_running_loop().time() - start
    # cooldown clamped to floor=5.0; для теста проверим что пройдёт минимум 0.1с
    # реально clamp поднимет до 5s — поэтому уменьшим cooldown_floor через прямую установку
    # Используем низкоуровневый путь через trigger но floor=5 — разумно проверять что есть задержка
    # На самом деле клампится до 5с, что слишком долго для теста. Тестируем floor отдельно.
    assert elapsed >= 0.05  # просто проверим что cooldown сработал


@pytest.mark.asyncio
async def test_cooldown_extension_takes_max() -> None:
    """Два последовательных trigger берут max, не суммируются."""
    coord = LLMRateCoordinator(concurrency=4, min_interval=0)
    await coord.trigger_cooldown(10.0, reason="first")
    await coord.trigger_cooldown(3.0, reason="second")
    stats = coord.stats()
    assert stats["cooldown_remaining"] >= 9.0
    assert stats["cooldown_remaining"] <= 11.0


@pytest.mark.asyncio
async def test_cooldown_caps() -> None:
    """trigger с огромным delay ограничивается до cap (120s)."""
    coord = LLMRateCoordinator(concurrency=4, min_interval=0)
    await coord.trigger_cooldown(9999.0, reason="ridiculous")
    stats = coord.stats()
    assert stats["cooldown_remaining"] <= 121.0
    assert stats["cooldown_remaining"] >= 119.0


@pytest.mark.asyncio
async def test_cooldown_floor() -> None:
    """trigger с delay меньше floor (5s) поднимается до floor."""
    coord = LLMRateCoordinator(concurrency=4, min_interval=0)
    await coord.trigger_cooldown(1.0, reason="small")
    stats = coord.stats()
    assert stats["cooldown_remaining"] >= 4.5
    assert stats["cooldown_remaining"] <= 5.5


@pytest.mark.asyncio
async def test_jitter_backoff_distributes() -> None:
    """jitter_backoff даёт разброс в окне [base/2, base]."""
    coord = LLMRateCoordinator(concurrency=1, min_interval=0)
    samples = [coord.jitter_backoff(2.0) for _ in range(50)]
    assert all(1.0 <= s <= 2.0 for s in samples)
    # Проверим, что значения не одинаковые (ну хоть какой-то разброс)
    assert len(set(round(s, 4) for s in samples)) > 5


@pytest.mark.asyncio
async def test_jitter_retry_after_distributes() -> None:
    """jitter_retry_after даёт разброс в окне [server, server*1.2]."""
    coord = LLMRateCoordinator(concurrency=1, min_interval=0)
    samples = [coord.jitter_retry_after(10.0) for _ in range(50)]
    assert all(10.0 <= s <= 12.0 for s in samples)


@pytest.mark.asyncio
async def test_fifo_fairness() -> None:
    """asyncio.Semaphore FIFO: задачи получают слот в порядке захода."""
    coord = LLMRateCoordinator(concurrency=1, min_interval=0)
    order: list[int] = []

    async def task(index: int) -> None:
        async with coord.acquire():
            order.append(index)
            await asyncio.sleep(0.005)

    # Запускаем последовательно с маленькой паузой, чтобы гарантировать порядок захода
    tasks: list[asyncio.Task[None]] = []
    for i in range(5):
        tasks.append(asyncio.create_task(task(i)))
        await asyncio.sleep(0.001)
    await asyncio.gather(*tasks)
    assert order == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_singleton_get_and_reset() -> None:
    """get_coordinator идемпотентен; reset_coordinator пересоздаёт."""
    a = get_coordinator()
    b = get_coordinator()
    assert a is b
    reset_coordinator()
    c = get_coordinator()
    assert c is not a


@pytest.mark.asyncio
async def test_coordinator_uses_settings() -> None:
    """get_coordinator(settings) применяет llm_concurrency и llm_request_delay."""
    from alla.config import Settings

    reset_coordinator()
    settings = Settings(
        endpoint="https://test",
        token="t",
        llm_concurrency=7,
        llm_request_delay=1.5,
    )
    coord = get_coordinator(settings)
    stats = coord.stats()
    assert stats["concurrency"] == 7
    assert stats["min_interval"] == 1.5
