"""Скриншоты из вложений: отбор в ядре, скачивание, раздел задания, проверка цитат, отчёт."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from skill_fixtures import project_fixture, testops_fixture, without_libmagic  # noqa: F401
from skill_fake_testops import PAY_MESSAGE, FakeTestOps, screenshot_launch, tiny_png
from test_skill_flow import _prepare

from alla_core.models.testops import (
    AttachmentMeta,
    ExecutionStep,
    FailedTestSummary,
    ImageAttachmentRef,
)
from alla_core.models.common import TestStatus as Status
from alla_core.services.image_attachments import image_attachments, is_image_attachment
from alla_skill_lib.analysis_format import parse_analysis, validate_analysis
from alla_skill_lib.cluster_task import IMAGES_HEADER, IMAGES_NOTE
from alla_skill_lib.images import fetch_images, image_type, select_image
from alla_skill_lib.report import IMAGE_QUOTE_NOTE, _evidence_lines
from alla_skill_lib.sources import describe, load_registry


@pytest.fixture(name="shop")
def shop_fixture(monkeypatch: pytest.MonkeyPatch) -> FakeTestOps:
    fake = FakeTestOps(screenshot_launch())
    fake.install(monkeypatch)
    return fake


# --- ядро: какие вложения — изображения и к какому шагу ---------------------------------


@pytest.mark.parametrize(("meta", "expected"), [
    ({"type": "image/png"}, True),
    ({"contentType": "image/jpeg; charset=binary"}, True),
    ({"type": "image/webp"}, True),
    ({"type": "image/svg+xml", "name": "logo.svg"}, False),
    ({"type": "text/plain", "name": "shot.png"}, False),
    ({"name": "screen.JPG"}, True),
    ({"name": "screen.svg"}, False),
    ({"name": "app.log"}, False),
])
def test_image_attachment_by_mime_then_extension(meta: dict, expected: bool) -> None:
    assert is_image_attachment(AttachmentMeta.model_validate({"id": 1, **meta})) is expected


def test_image_step_comes_from_the_execution_tree() -> None:
    steps = [ExecutionStep.model_validate(step) for step in [
        {"name": "Открыть", "status": "passed",
         "attachments": [{"id": 1, "name": "open.png"}]},
        {"name": "Оплатить", "status": "failed", "steps": [
            {"name": "Нажать кнопку", "status": "passed",
             "attachments": [{"name": "click.png"}], "steps": [
                 {"name": "Дождаться формы", "status": "passed",
                  "attachments": [{"id": 5, "name": "deep.png"}]},
             ]},
        ]},
    ]]
    attachments = [AttachmentMeta.model_validate(item) for item in [
        {"id": 1, "name": "open.png", "type": "image/png"},
        {"id": 2, "name": "click.png", "type": "image/png"},
        {"id": 3, "name": "after.png", "type": "image/png"},
        {"id": 4, "name": "app.log", "type": "text/plain"},
        {"name": "no-id.png", "type": "image/png"},
        {"id": 5, "name": "deep.png", "type": "image/png"},
    ]]
    refs = image_attachments(attachments, steps)
    assert [(ref.id, ref.step, ref.failed_step) for ref in refs] == [
        (1, "Открыть", False),
        (2, "Нажать кнопку", True),  # без id в дереве — по имени; внутри упавшего шага
        (3, None, False),
        (5, "Дождаться формы", True),  # упавший предок через два уровня passed-шагов
    ]


def test_same_name_outside_the_tree_is_not_the_failed_step_screenshot() -> None:
    """Скриншот после теста (вне дерева) с тем же именем, что у упавшего шага, — не его."""
    steps = [ExecutionStep.model_validate({
        "name": "Оплатить", "status": "failed",
        "attachments": [{"id": 9401, "name": "screenshot.png"}]})]
    refs = image_attachments([AttachmentMeta.model_validate(item) for item in [
        {"id": 9401, "name": "screenshot.png", "type": "image/png"},
        {"id": 9402, "name": "screenshot.png", "type": "image/png"},
    ]], steps)
    assert [(ref.id, ref.step, ref.failed_step) for ref in refs] == [
        (9401, "Оплатить", True), (9402, None, False)]
    assert select_image(_test(*refs)) == refs[0]


def test_ambiguous_name_leaves_the_step_unknown() -> None:
    """В дереве ``screenshot.png`` без id, в списке — два вложения с этим именем: чьё оно,
    не узнать, шаг у обоих неизвестен, и после теста не вытесняет скриншот шага."""
    steps = [ExecutionStep.model_validate({
        "name": "Оплатить", "status": "failed", "attachments": [{"name": "screenshot.png"}]})]
    refs = image_attachments([AttachmentMeta.model_validate(item) for item in [
        {"id": 9401, "name": "screenshot.png", "type": "image/png"},
        {"id": 9402, "name": "screenshot.png", "type": "image/png"},
        {"id": 9403, "name": "app.log", "type": "text/plain"},
    ]], steps)
    assert [(ref.id, ref.step, ref.failed_step) for ref in refs] == [
        (9401, None, False), (9402, None, False)]
    single = image_attachments([AttachmentMeta.model_validate(
        {"id": 9401, "name": "screenshot.png", "type": "image/png"})], steps)
    assert [(ref.step, ref.failed_step) for ref in single] == [("Оплатить", True)]


def test_log_extraction_records_images_without_downloading_them(
    project: Path, shop: FakeTestOps, capsys,
) -> None:
    run_dir, _run, _out = _prepare(project, capsys, launch_id=791)
    downloaded = [path for _method, path in shop.requests if path.endswith("/content")]
    # Скачан только скриншот упавшего шага: ни раннего скриншота, ни SVG, ни картинки без
    # MIME (фильтр логов пропускает вложения без MIME — изображения из него исключены).
    assert downloaded == ["/api/testresult/attachment/9401/content"]
    assert (run_dir / "images" / "01" / "401.png").read_bytes() == tiny_png(3, 3)


# --- выбор и скачивание -----------------------------------------------------------------


def _test(*refs: ImageAttachmentRef) -> FailedTestSummary:
    return FailedTestSummary(test_result_id=7, name="payOrder", status=Status.FAILED,
                             image_attachments=list(refs))


def test_select_image_prefers_failed_step_then_latest() -> None:
    early = ImageAttachmentRef(id=5, name="a.png")
    failed = ImageAttachmentRef(id=3, name="b.png", step="Оплатить", failed_step=True)
    late = ImageAttachmentRef(id=9, name="c.png")
    assert select_image(_test(early, failed, late)) == failed
    assert select_image(_test(early, late)) == late
    assert select_image(_test()) is None


def test_image_type_trusts_bytes_not_mime() -> None:
    assert image_type(tiny_png()) == "png"
    assert image_type(b"\xff\xd8\xff\xe0rest") == "jpg"
    assert image_type(b"GIF89a....") == "gif"
    assert image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert image_type(b"<svg/>") is None


class _Client:
    def __init__(self, contents: dict[int, bytes]) -> None:
        self.contents = contents
        self.asked: list[tuple[int, int | None]] = []

    async def get_attachment_content(self, attachment_id: int, *,
                                     max_bytes: int | None = None) -> bytes:
        self.asked.append((attachment_id, max_bytes))
        if attachment_id not in self.contents:
            raise RuntimeError("404")
        data = self.contents[attachment_id]
        return data if max_bytes is None else data[:max_bytes]


def test_fetch_images_skips_big_broken_and_failed(tmp_path: Path) -> None:
    def test(test_id: int, ref: ImageAttachmentRef) -> tuple[FailedTestSummary, ImageAttachmentRef]:
        return FailedTestSummary(test_result_id=test_id, name=f"t{test_id}",
                                 status=Status.FAILED), ref

    plan = [
        test(1, ImageAttachmentRef(id=11, name="ok.png", step="Шаг")),
        test(2, ImageAttachmentRef(id=12, name="big.png", size=10_000)),
        test(3, ImageAttachmentRef(id=13, name="big-no-size.png")),
        test(4, ImageAttachmentRef(id=14, name="fake.png")),
        test(5, ImageAttachmentRef(id=15, name="missing.png")),
    ]
    client = _Client({11: tiny_png(), 12: tiny_png(), 13: tiny_png() + b"x" * 200,
                      14: b"not an image"})
    staged = asyncio.run(fetch_images(client, plan, tmp_path, max_bytes=150, concurrency=2))
    assert list(staged) == [1]
    assert staged[1].path.read_bytes() == tiny_png() and staged[1].step == "Шаг"
    assert 12 not in {attachment for attachment, _ in client.asked}  # размер известен заранее
    assert all(limit == 151 for _, limit in client.asked)


def test_partial_screenshot_is_removed_after_write_error(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Место кончилось посреди записи — неполный файл удаляется сразу, а не держит место."""
    real_write = Path.write_bytes

    def half_write(self: Path, data: bytes) -> int:
        real_write(self, data[:5])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", half_write)
    plan = [(FailedTestSummary(test_result_id=1, name="t1", status=Status.FAILED),
             ImageAttachmentRef(id=11, name="ok.png"))]
    staged = asyncio.run(fetch_images(_Client({11: tiny_png()}), plan, tmp_path,
                                      max_bytes=10_000, concurrency=1))
    assert staged == {} and list(tmp_path.iterdir()) == []


def test_images_can_be_turned_off(project: Path, shop: FakeTestOps, capsys,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLURE_IMAGES_PER_PROBLEM", "0")
    run_dir, run, _out = _prepare(project, capsys, launch_id=791)
    assert not any(path.endswith("/content") for _method, path in shop.requests)
    assert not (run_dir / "images").exists() and "images" not in run["clusters"][0]
    assert IMAGES_HEADER not in (run_dir / "clusters" / "01.md").read_text(encoding="utf-8")


def test_unwritable_screenshot_is_skipped_not_fatal(project: Path, shop: FakeTestOps, capsys,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """Нет места во временной папке или в папке разбора — разбор без скриншота, а не сбой."""
    def refuse(self: Path, data: bytes) -> int:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", refuse)
    run_dir, run, _out = _prepare(project, capsys, launch_id=791)
    assert not run["clusters"][0]["auto"] and "images" not in run["clusters"][0]
    assert IMAGES_HEADER not in (run_dir / "clusters" / "01.md").read_text(encoding="utf-8")


def test_screenshot_not_placed_in_run_dir_is_left_out(project: Path, shop: FakeTestOps, capsys,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from alla_skill_lib import images, workspace

    real = workspace.write_atomic_bytes

    def refuse_images(path: Path, data: bytes, **kwargs: object) -> None:
        if "images" in path.parts:
            raise OSError(13, "Permission denied")
        real(path, data, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(images.ws, "write_atomic_bytes", refuse_images)
    run_dir, run, _out = _prepare(project, capsys, launch_id=791)
    assert not run["clusters"][0]["auto"] and "images" not in run["clusters"][0]
    assert IMAGES_HEADER not in (run_dir / "clusters" / "01.md").read_text(encoding="utf-8")


def test_reviewer_checks_screenshot_quotes_by_the_image() -> None:
    from alla_skill_lib.review_model import SYSTEM_PROMPT

    text = " ".join(SYSTEM_PROMPT.split())
    assert "Тебе приложены те же" in text and "сверяй с изображением" in text
    assert "не считай ни подтверждением, ни ошибкой" in text  # если изображения нет
    assert "держится на скриншоте, проверить нельзя" in text


# --- задание и реестр -------------------------------------------------------------------


def test_task_shows_screenshot_as_next_source(project: Path, shop: FakeTestOps, capsys) -> None:
    run_dir, run, _out = _prepare(project, capsys, launch_id=791)
    task = (run_dir / "clusters" / "01.md").read_text(encoding="utf-8")
    image = run_dir / "images" / "01" / "401.png"
    assert IMAGES_HEADER in task and IMAGES_NOTE in task
    assert "--- [S3 · скриншот screenshot.png · шаг «Оплатить заказ» · тест payOrder] ---\n" \
           f"{image}\n" in task
    # Раздел — после «Данных», до списка тестов и кода.
    assert task.index(PAY_MESSAGE) < task.index(IMAGES_HEADER) < task.index("## Задание")
    registry = load_registry(run_dir / "evidence" / "01.sources.json")
    assert registry is not None
    assert registry["S3"] | {"text": None} == {
        "kind": "image", "test_result_id": 401, "test_name": "payOrder", "section": "скриншот",
        "attachment": "screenshot.png", "attachment_id": 9401, "lines": None, "text": None,
        "file": "images/01/401.png", "step": "Оплатить заказ",
    }
    assert run["clusters"][0]["images"] == [{
        "source_id": "S3", "file": "images/01/401.png", "test_result_id": 401,
        "name": "screenshot.png", "step": "Оплатить заказ",
    }]


def test_task_without_images_has_no_screenshot_section(project: Path, testops: FakeTestOps,
                                                       capsys) -> None:
    run_dir, run, _out = _prepare(project, capsys)
    for entry in run["clusters"]:
        assert "images" not in entry
        task = run_dir / "clusters" / f"{entry['file_id']}.md"
        if task.exists():
            assert IMAGES_HEADER not in task.read_text(encoding="utf-8")


# --- проверка разбора и отчёт ------------------------------------------------------------


SOURCES = {
    "S1": {"kind": "message", "text": PAY_MESSAGE},
    "S2": {"kind": "image", "text": "", "section": "скриншот", "attachment": "screenshot.png",
           "test_name": "payOrder", "step": "Оплатить заказ", "file": "images/01/401.png"},
}


def _analysis(category: str, quote: str = "Мы используем cookie") -> str:
    return (
        "ЧТО СЛОМАЛОСЬ: Тест не дождался кнопки: её закрывает окно cookie.\n"
        f"ПРИЧИНА: {category} — окно cookie перекрывает кнопку оплаты.\n"
        "НАБЛЮДЕНИЯ:\n"
        f"- [S2] «{quote}» — окно поверх формы\n"
        "НЕ ХВАТАЕТ: нет\n"
        "КАК ИСПРАВИТЬ:\n1. Принимать cookie в начале теста.\n"
    )


def test_screenshot_quote_is_accepted_without_matching(project: Path) -> None:
    analysis = parse_analysis(_analysis("тест"))
    assert validate_analysis(analysis, project, frozenset(), observed=True, sources=SOURCES) == []
    short = parse_analysis(_analysis("тест", quote="ок"))
    assert any("слишком короткая" in error for error in validate_analysis(
        short, project, frozenset(), observed=True, sources=SOURCES))


def test_screenshot_does_not_confirm_application(project: Path) -> None:
    analysis = parse_analysis(_analysis("приложение"))
    errors = validate_analysis(analysis, project, frozenset(), observed=True, sources=SOURCES)
    assert any("есть только цитаты со скриншота" in error for error in errors)
    environment = parse_analysis(_analysis("окружение"))
    environment.sources = SOURCES
    assert environment.unconfirmed_by_log


def test_report_names_screenshot_and_links_file() -> None:
    assert describe(SOURCES["S2"]) == "скриншот screenshot.png, шаг «Оплатить заказ», тест payOrder"
    analysis = parse_analysis(_analysis("тест"))
    analysis.sources = SOURCES
    line = next(line for line in _evidence_lines(analysis, "Наблюдения:", "Не хватает: ")
                if "cookie" in line)
    assert IMAGE_QUOTE_NOTE in line and "[images/01/401.png](images/01/401.png)" in line


def test_run_json_stays_without_image_metadata(project: Path, shop: FakeTestOps, capsys) -> None:
    run_dir, _run, _out = _prepare(project, capsys, launch_id=791)
    raw = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert all("image_attachments" not in test for test in raw["triage"]["failed_tests"])
