"""Вложения-изображения упавшего теста: только метаданные, без скачивания.

Скриншоты в логи не попадают (бинарные вложения извлечение логов пропускает), но модель
может посмотреть их сама: скилл скачивает по одному изображению на тест-пример кластера.
Здесь — какие вложения считаются изображениями и к какому шагу дерева они приложены.
"""

from __future__ import annotations

import os

from alla_core.models.testops import AttachmentMeta, ExecutionStep, ImageAttachmentRef

IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_FAILURE_STATUSES = frozenset({"failed", "broken"})


def is_image_attachment(att: AttachmentMeta) -> bool:
    """PNG, JPEG, GIF или WebP по MIME или, без MIME, по расширению; SVG — нет (это текст)."""
    mime = (att.type or att.content_type or "").lower().split(";")[0].strip()
    if mime:
        return mime in IMAGE_MIMES
    name = (att.name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS


def _step_places(
    steps: list[ExecutionStep],
    failed_above: bool = False,
) -> tuple[dict[int, tuple[str | None, bool]], dict[str, list[tuple[str | None, bool]]]]:
    """Шаг каждого вложения дерева: по id и по имени (имя — когда id в дереве нет)."""
    by_id: dict[int, tuple[str | None, bool]] = {}
    by_name: dict[str, list[tuple[str | None, bool]]] = {}
    for step in steps:
        failed = bool(step.status and step.status.lower() in _FAILURE_STATUSES)
        place = (step.name, failed or failed_above)
        for att in step.attachments or []:
            if att.id is not None:
                by_id.setdefault(att.id, place)
            if att.name:
                by_name.setdefault(att.name, []).append(place)
        if step.steps:
            inner_ids, inner_names = _step_places(step.steps, failed)
            for key, value in inner_ids.items():
                by_id.setdefault(key, value)
            for name, places in inner_names.items():
                by_name.setdefault(name, []).extend(places)
    return by_id, by_name


def image_attachments(
    attachments: list[AttachmentMeta],
    steps: list[ExecutionStep] | None,
) -> list[ImageAttachmentRef]:
    """Изображения теста с id, в порядке списка TestOps; шаг — из дерева исполнения.

    ``failed_step`` — изображение приложено к упавшему шагу (или внутри него). Вложение
    без id в дереве узнаётся по имени, только если такое имя в дереве одно.
    """
    by_id, by_name = _step_places(steps or [])
    result: list[ImageAttachmentRef] = []
    for att in attachments:
        if att.id is None or not is_image_attachment(att):
            continue
        place = by_id.get(att.id)
        if place is None and att.name and len(by_name.get(att.name, [])) == 1:
            place = by_name[att.name][0]
        step, failed = place if place is not None else (None, False)
        result.append(ImageAttachmentRef(
            id=att.id,
            name=(att.name or f"attachment-{att.id}").replace("\n", " ").replace("\r", " ").strip(),
            mime=(att.type or att.content_type or "").lower().split(";")[0].strip() or None,
            size=att.size,
            step=step,
            failed_step=failed,
        ))
    return result
