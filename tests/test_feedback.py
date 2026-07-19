"""反馈闭环（feedback.py）离线单测：权重更新、状态流转、SM-2 入队。"""

from datetime import date, timedelta

import pytest

from app.config import settings
from app.db import get_db, init_db
from app.feedback import apply_feedback


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """指向临时数据库，避免污染真实 data/lyrien.db。"""
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path}/test.db")
    init_db()
    yield tmp_path / "test.db"


def _make_card(tags: str, source_id: int = 1) -> int:
    """造一个 item + card，返回 card_id。默认信源为 init_db 内置的 sources。"""
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO items (source_id, title, url, summary, url_hash)
            VALUES (?, '测试文章', ?, '摘要', ?)
            """,
            (source_id, f"https://example.com/{tags}", f"hash-{tags}"),
        )
        item_id = cur.lastrowid
        cur = conn.execute(
            """
            INSERT INTO cards (date, item_id, title, summary_json, tags)
            VALUES (?, ?, '测试卡片', '{}', ?)
            """,
            (date.today().isoformat(), item_id, tags),
        )
        conn.commit()
        return cur.lastrowid


def _get_weight(table: str, name: str) -> float:
    with get_db() as conn:
        row = conn.execute(
            f"SELECT weight FROM {table} WHERE name = ?", (name,)
        ).fetchone()
    return row["weight"]


def test_up_feedback_raises_tag_and_source_weights(fresh_db):
    # init_db 默认：网络安全 1.0、C++ 1.0；Hacker News(id=1) weight 1.0
    card_id = _make_card("网络安全, C++", source_id=1)

    assert apply_feedback(card_id, "up") is True

    assert _get_weight("tags", "网络安全") == pytest.approx(1.1)
    assert _get_weight("tags", "C++") == pytest.approx(1.1)
    with get_db() as conn:
        source = conn.execute("SELECT weight FROM sources WHERE id = 1").fetchone()
        card = conn.execute("SELECT status FROM cards WHERE id = ?", (card_id,)).fetchone()
        fb = conn.execute(
            "SELECT action FROM feedback WHERE card_id = ?", (card_id,)
        ).fetchone()
    assert source["weight"] == pytest.approx(1.05)
    assert card["status"] == "liked"
    assert fb["action"] == "up"


def test_down_feedback_lowers_weights_with_floor(fresh_db):
    # 新标签默认 0.0 起步，down 后应被钳制在 0，而不是负数
    card_id = _make_card("陌生话题", source_id=2)

    assert apply_feedback(card_id, "down") is True

    assert _get_weight("tags", "陌生话题") == 0.0
    with get_db() as conn:
        source = conn.execute("SELECT weight FROM sources WHERE id = 2").fetchone()
        card = conn.execute("SELECT status FROM cards WHERE id = ?", (card_id,)).fetchone()
    assert source["weight"] == pytest.approx(0.9)
    assert card["status"] == "disliked"


def test_mastered_enters_review_queue(fresh_db):
    card_id = _make_card("机器学习", source_id=1)
    before = _get_weight("tags", "机器学习")

    assert apply_feedback(card_id, "mastered") is True

    with get_db() as conn:
        card = conn.execute(
            "SELECT status, ease, interval, due_date FROM cards WHERE id = ?",
            (card_id,),
        ).fetchone()
    assert card["status"] == "mastered"
    assert card["ease"] == pytest.approx(2.5)
    assert card["interval"] == 1
    assert card["due_date"] == (date.today() + timedelta(days=1)).isoformat()
    # 已掌握不改变标签权重
    assert _get_weight("tags", "机器学习") == before


def test_feedback_on_missing_card_returns_false(fresh_db):
    assert apply_feedback(9999, "up") is False


def test_invalid_action_raises(fresh_db):
    card_id = _make_card("C++", source_id=1)
    with pytest.raises(ValueError):
        apply_feedback(card_id, "bogus")
