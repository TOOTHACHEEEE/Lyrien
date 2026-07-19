"""简化 SM-2 复习队列（review.py）离线单测：到期筛选与评分流转。"""

from datetime import date, timedelta

import pytest

from app.config import settings
from app.db import get_db, init_db
from app.review import due_count, get_due_cards, grade_card, mastered_count

TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
TOMORROW = (date.today() + timedelta(days=1)).isoformat()


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """指向临时数据库，避免污染真实 data/lyrien.db。"""
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path}/test.db")
    init_db()
    yield tmp_path / "test.db"


def _make_card(
    status: str = "mastered",
    ease: float | None = 2.5,
    interval: int | None = 1,
    due_date: str | None = TODAY,
) -> int:
    """造一个 item + card，返回 card_id。"""
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO items (source_id, title, url, summary, url_hash)
            VALUES (1, '测试文章', ?, '摘要', ?)
            """,
            (f"https://example.com/{status}-{due_date}-{interval}", f"hash-{status}-{due_date}-{interval}"),
        )
        item_id = cur.lastrowid
        cur = conn.execute(
            """
            INSERT INTO cards (date, item_id, title, summary_json, status, ease, interval, due_date)
            VALUES (?, ?, '测试卡片', '{}', ?, ?, ?, ?)
            """,
            (TODAY, item_id, status, ease, interval, due_date),
        )
        conn.commit()
        return cur.lastrowid


def _get_card(card_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, ease, interval, due_date FROM cards WHERE id = ?",
            (card_id,),
        ).fetchone()
    return dict(row)


def test_get_due_cards_only_returns_due_mastered(fresh_db):
    due_today = _make_card(due_date=TODAY)
    overdue = _make_card(due_date=YESTERDAY, interval=3)
    _make_card(due_date=TOMORROW, interval=5)          # 未到期
    _make_card(status="new", due_date=None, ease=None, interval=None)  # 不在队列

    due_ids = [c["id"] for c in get_due_cards()]
    assert due_today in due_ids
    assert overdue in due_ids
    assert len(due_ids) == 2
    assert due_count() == 2
    # mastered 计数只统计在队列中的卡片
    assert mastered_count() == 3


def test_grade_remember_grows_interval_and_ease(fresh_db):
    card_id = _make_card(ease=2.5, interval=3, due_date=TODAY)

    result = grade_card(card_id, "remember")

    assert result is not None
    assert result["interval"] == 8  # round(3 * 2.5)
    assert result["ease"] == pytest.approx(2.55)
    assert result["due_date"] == (date.today() + timedelta(days=8)).isoformat()
    card = _get_card(card_id)
    assert card["due_date"] == result["due_date"]
    # 评分流水写入 feedback 表
    with get_db() as conn:
        fb = conn.execute(
            "SELECT action FROM feedback WHERE card_id = ?", (card_id,)
        ).fetchone()
    assert fb["action"] == "review:remember"


def test_grade_fuzzy_soft_growth_with_ease_floor(fresh_db):
    card_id = _make_card(ease=1.4, interval=5, due_date=TODAY)

    result = grade_card(card_id, "fuzzy")

    assert result["interval"] == 6  # round(5 * 1.2)
    assert result["ease"] == pytest.approx(1.3)  # 不低于下限


def test_grade_forgot_resets_interval(fresh_db):
    card_id = _make_card(ease=1.5, interval=20, due_date=TODAY)

    result = grade_card(card_id, "forgot")

    assert result["interval"] == 1
    assert result["ease"] == pytest.approx(1.3)
    assert result["due_date"] == TOMORROW
    # 忘记后明天重新到期
    assert due_count(today=date.today() + timedelta(days=1)) == 1


def test_grade_ease_never_exceeds_cap(fresh_db):
    card_id = _make_card(ease=2.79, interval=2, due_date=TODAY)
    result = grade_card(card_id, "remember")
    assert result["ease"] == pytest.approx(2.8)


def test_grade_rejects_card_not_in_queue(fresh_db):
    new_card = _make_card(status="new", due_date=None, ease=None, interval=None)
    assert grade_card(new_card, "remember") is None
    assert grade_card(9999, "remember") is None


def test_grade_invalid_grade_raises(fresh_db):
    card_id = _make_card()
    with pytest.raises(ValueError):
        grade_card(card_id, "perfect")
