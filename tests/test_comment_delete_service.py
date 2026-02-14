"""Тесты CommentDeleteService: фильтрация по префиксу, dry run, error resilience."""

from __future__ import annotations

import pytest

from alla.models.testops import CommentResponse
from alla.services.comment_delete_service import CommentDeleteService
from conftest import make_comment_response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockCommentManager:
    """Заглушка для CommentManager с настраиваемыми ошибками."""

    def __init__(
        self,
        comments_by_tc: dict[int, list[CommentResponse]] | None = None,
        *,
        fail_get: set[int] | None = None,
        fail_delete: set[int] | None = None,
    ) -> None:
        self.get_calls: list[int] = []
        self.delete_calls: list[int] = []
        self.comments_by_tc = comments_by_tc or {}
        self.fail_get = fail_get or set()
        self.fail_delete = fail_delete or set()

    async def get_comments(self, test_case_id: int) -> list[CommentResponse]:
        self.get_calls.append(test_case_id)
        if test_case_id in self.fail_get:
            raise RuntimeError(f"get_comments failed for {test_case_id}")
        return self.comments_by_tc.get(test_case_id, [])

    async def delete_comment(self, comment_id: int) -> None:
        self.delete_calls.append(comment_id)
        if comment_id in self.fail_delete:
            raise RuntimeError(f"delete_comment failed for {comment_id}")


# ---------------------------------------------------------------------------
# Пустой ввод
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_set_returns_zero_counts() -> None:
    """test_case_ids=set() → все counts=0, никаких вызовов."""
    manager = _MockCommentManager()
    service = CommentDeleteService(manager)  # type: ignore[arg-type]

    result = await service.delete_alla_comments(set())

    assert result.total_test_cases == 0
    assert result.comments_found == 0
    assert result.comments_deleted == 0
    assert result.comments_failed == 0
    assert len(manager.get_calls) == 0


# ---------------------------------------------------------------------------
# Фильтрация по префиксу [alla]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filters_by_alla_prefix() -> None:
    """Только комментарии с префиксом '[alla]' включаются, остальные — нет."""
    manager = _MockCommentManager(comments_by_tc={
        100: [
            make_comment_response(id=1, body="[alla] LLM analysis"),
            make_comment_response(id=2, body="Regular comment"),
            make_comment_response(id=3, body="[alla] KB recommendation"),
        ],
    })
    service = CommentDeleteService(manager)  # type: ignore[arg-type]

    result = await service.delete_alla_comments({100})

    assert result.comments_found == 2
    assert sorted(manager.delete_calls) == [1, 3]


@pytest.mark.asyncio
async def test_strips_whitespace_before_prefix_check() -> None:
    """Пробел перед '[alla]' → комментарий включён (lstrip)."""
    manager = _MockCommentManager(comments_by_tc={
        100: [
            make_comment_response(id=1, body="  [alla] with leading spaces"),
        ],
    })
    service = CommentDeleteService(manager)  # type: ignore[arg-type]

    result = await service.delete_alla_comments({100})

    assert result.comments_found == 1
    assert manager.delete_calls == [1]


@pytest.mark.asyncio
async def test_case_sensitive_prefix() -> None:
    """'[ALLA]' (uppercase) → НЕ совпадает, только '[alla]' (lowercase)."""
    manager = _MockCommentManager(comments_by_tc={
        100: [
            make_comment_response(id=1, body="[ALLA] uppercase"),
            make_comment_response(id=2, body="[Alla] mixed"),
        ],
    })
    service = CommentDeleteService(manager)  # type: ignore[arg-type]

    result = await service.delete_alla_comments({100})

    assert result.comments_found == 0
    assert manager.delete_calls == []


@pytest.mark.asyncio
async def test_none_body_skipped() -> None:
    """body=None → комментарий пропущен (не падает)."""
    manager = _MockCommentManager(comments_by_tc={
        100: [
            make_comment_response(id=1, body=None),
        ],
    })
    service = CommentDeleteService(manager)  # type: ignore[arg-type]

    result = await service.delete_alla_comments({100})

    assert result.comments_found == 0


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_scans_but_not_deletes() -> None:
    """dry_run=True → comments_found > 0, comments_deleted=0."""
    manager = _MockCommentManager(comments_by_tc={
        100: [make_comment_response(id=1, body="[alla] text")],
        200: [make_comment_response(id=2, body="[alla] text")],
    })
    service = CommentDeleteService(manager)  # type: ignore[arg-type]

    result = await service.delete_alla_comments({100, 200}, dry_run=True)

    assert result.comments_found == 2
    assert result.comments_deleted == 0
    assert len(manager.delete_calls) == 0


# ---------------------------------------------------------------------------
# Two-phase execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_aggregates_all_comments() -> None:
    """3 tc_id по 2 alla-комментария → found=6."""
    comments = {}
    for tc_id in [100, 200, 300]:
        comments[tc_id] = [
            make_comment_response(id=tc_id * 10 + 1, body="[alla] first"),
            make_comment_response(id=tc_id * 10 + 2, body="[alla] second"),
        ]
    manager = _MockCommentManager(comments_by_tc=comments)
    service = CommentDeleteService(manager)  # type: ignore[arg-type]

    result = await service.delete_alla_comments({100, 200, 300})

    assert result.comments_found == 6
    assert result.comments_deleted == 6
    assert len(manager.delete_calls) == 6


@pytest.mark.asyncio
async def test_delete_calls_for_each_comment() -> None:
    """found=3 → 3 вызова delete_comment с правильными ID."""
    manager = _MockCommentManager(comments_by_tc={
        100: [
            make_comment_response(id=10, body="[alla] a"),
            make_comment_response(id=11, body="[alla] b"),
            make_comment_response(id=12, body="[alla] c"),
        ],
    })
    service = CommentDeleteService(manager)  # type: ignore[arg-type]

    await service.delete_alla_comments({100})

    assert sorted(manager.delete_calls) == [10, 11, 12]


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_error_does_not_stop_others() -> None:
    """get_comments raises для одного tc_id → 0 комментариев от него, остальные ok."""
    manager = _MockCommentManager(
        comments_by_tc={
            200: [make_comment_response(id=1, body="[alla] ok")],
        },
        fail_get={100},
    )
    service = CommentDeleteService(manager)  # type: ignore[arg-type]

    result = await service.delete_alla_comments({100, 200})

    assert result.comments_found == 1
    assert result.comments_deleted == 1


@pytest.mark.asyncio
async def test_delete_error_increments_failed() -> None:
    """delete_comment raises для одного id → comments_failed=1, остальные deleted."""
    manager = _MockCommentManager(
        comments_by_tc={
            100: [
                make_comment_response(id=10, body="[alla] a"),
                make_comment_response(id=11, body="[alla] b"),
                make_comment_response(id=12, body="[alla] c"),
            ],
        },
        fail_delete={11},
    )
    service = CommentDeleteService(manager)  # type: ignore[arg-type]

    result = await service.delete_alla_comments({100})

    assert result.comments_found == 3
    assert result.comments_deleted == 2
    assert result.comments_failed == 1
