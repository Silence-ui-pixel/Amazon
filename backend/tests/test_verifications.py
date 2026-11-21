"""自动核验的选择规则和失败语义，不访问外网。"""

from uuid import uuid4

from app.domain.verification import ItemStatus, VerificationItem
from app.services.verification import choose_best


def item(
    page: int | None, position: int | None, status: ItemStatus = "matched"
) -> VerificationItem:
    return VerificationItem(
        candidate_id=uuid4(),
        text="cup",
        translation="杯子",
        status=status,
        page=page,
        position=position,
        checked_pages=1,
    )


def test_best_uses_page_then_organic_position_and_keeps_ties() -> None:
    a, b, c = item(2, 1), item(1, 8), item(1, 8)
    best, complete = choose_best([a, b, c])
    assert best == [b.candidate_id, c.candidate_id]
    assert complete


def test_partial_failure_cannot_be_final_or_pick_not_found() -> None:
    found = item(1, 2)
    states: list[ItemStatus] = ["failed", "uncertain", "needs_reference", "waiting"]
    for state in states:
        best, complete = choose_best([found, item(None, None, state)])
        assert best == [found.candidate_id]
        assert not complete
    assert choose_best([item(None, None, "not_found_in_scope")]) == ([], True)
