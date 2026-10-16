"""Скриншоты из вложений тестов: какие скачать и как.

Модель смотрит изображения сама (``read_file`` в Qwen Code отдаёт PNG/JPEG/GIF/WebP как
картинку), поэтому скилл кладёт в папку разбора по одному изображению на тест, чьи данные
увидит модель (``cluster_task.shown_tests``), не больше ``images_per_problem`` на проблему.
В кластеризации, сигнатуре и базе знаний изображения не участвуют.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from alla_core.models.clustering import FailureCluster
from alla_core.models.testops import FailedTestSummary, ImageAttachmentRef

from alla_skill_lib.cluster_task import has_evidence, select_log_and_trace, shown_tests

logger = logging.getLogger(__name__)


class ImageSource(Protocol):
    async def get_attachment_content(
        self, attachment_id: int, *, max_bytes: int | None = None,
    ) -> bytes: ...


@dataclass(frozen=True)
class StagedImage:
    """Скачанный скриншот теста во временной папке ``prepare``."""

    test_result_id: int
    path: Path
    name: str
    extension: str
    step: str | None = None


def image_type(data: bytes) -> str | None:
    """Расширение по сигнатуре байтов (MIME из TestOps бывает неверным); ``None`` — не картинка."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def select_image(test: FailedTestSummary) -> ImageAttachmentRef | None:
    """Последнее изображение упавшего шага, иначе последнее приложенное (наибольший id):
    скриншот при падении обычно снимают в самом шаге или после теста (afterEach)."""
    refs = test.image_attachments
    pool = [ref for ref in refs if ref.failed_step] or refs
    return max(pool, key=lambda ref: ref.id) if pool else None


def plan_images(
    clusters: list[FailureCluster],
    tests_by_id: dict[int, FailedTestSummary],
    limit: int,
) -> list[tuple[FailedTestSummary, ImageAttachmentRef]]:
    """Что скачать: по изображению на показанный тест, до ``limit`` на кластер.

    Кластеры без данных (``auto``) задания не получают — и скриншотов тоже.
    """
    if limit <= 0:
        return []
    plan: list[tuple[FailedTestSummary, ImageAttachmentRef]] = []
    for cluster in clusters:
        log_snippet, _trace = select_log_and_trace(cluster, tests_by_id)
        if not has_evidence(cluster, log_snippet):
            continue
        taken = 0
        for test in shown_tests(cluster, tests_by_id, log_snippet):
            ref = select_image(test)
            if ref is None:
                continue
            plan.append((test, ref))
            taken += 1
            if taken >= limit:
                break
    return plan


async def fetch_images(
    client: ImageSource,
    plan: list[tuple[FailedTestSummary, ImageAttachmentRef]],
    staging: Path,
    *,
    max_bytes: int,
    concurrency: int,
) -> dict[int, StagedImage]:
    """Скачать скриншоты во временную папку; id результата теста → файл.

    Больше ``max_bytes`` (по метаданным или по факту), не картинка или сбой скачивания —
    изображение пропускается: без скриншота разбор прежний.
    """
    semaphore = asyncio.Semaphore(max(1, concurrency))
    staged: dict[int, StagedImage] = {}

    async def fetch(test: FailedTestSummary, ref: ImageAttachmentRef) -> None:
        if ref.size is not None and ref.size > max_bytes:
            logger.info("Скриншот %d (%s) больше %d байт — пропущен", ref.id, ref.name, max_bytes)
            return
        async with semaphore:
            try:
                # На байт больше потолка: так видно, что файл обрезан бы был.
                data = await client.get_attachment_content(ref.id, max_bytes=max_bytes + 1)
            except Exception as exc:
                logger.warning("Скриншот %d (%s) теста %d не скачан: %s",
                               ref.id, ref.name, test.test_result_id, exc)
                return
        if len(data) > max_bytes:
            logger.info("Скриншот %d (%s) больше %d байт — пропущен", ref.id, ref.name, max_bytes)
            return
        extension = image_type(data)
        if extension is None:
            logger.info("Вложение %d (%s) — не PNG, JPEG, GIF или WebP, пропущено",
                        ref.id, ref.name)
            return
        path = staging / f"{test.test_result_id}.{extension}"
        path.write_bytes(data)
        staged[test.test_result_id] = StagedImage(test.test_result_id, path, ref.name,
                                                  extension, ref.step)

    await asyncio.gather(*(fetch(test, ref) for test, ref in plan))
    return staged
