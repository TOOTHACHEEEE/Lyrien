"""学习周报（report.py）离线单测：周界、统计汇总、快照、到期判断、生成落库。"""

from datetime import date, datetime

import pytest

import app.report as report
from app.config import settings
from app.db import get_db, init_db

# 2024-01-01 是周一，2024-01-07 是周日
WEEK_START = "2024-01-01"
WEEK_END = "2024-01-07"


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """指向临时数据库，避免污染真实 data/lyrien.db。"""
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path}/test.db")
    init_db()
    yield tmp_path / "test.db"


def _make_card(day: str, title: str = "测试卡片", tags: str = "网络安全") -> int:
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO items (source_id, title, url, summary, url_hash)
            VALUES (1, ?, ?, '摘要', ?)
            """,
            (title, f"https://example.com/{day}-{title}", f"hash-{day}-{title}"),
        )
        item_id = cur.lastrowid
        cur = conn.execute(
            """
            INSERT INTO cards (date, item_id, title, summary_json, tags, status)
            VALUES (?, ?, ?, '{}', ?, 'mastered')
            """,
            (day, item_id, title, tags),
        )
        conn.commit()
        return cur.lastrowid


def _make_feedback(card_id: int, action: str, created_at: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO feedback (card_id, action, created_at) VALUES (?, ?, ?)",
            (card_id, action, created_at),
        )
        conn.commit()


def test_week_bounds_properties():
    monday, sunday = report.week_bounds(date(2024, 1, 3))
    assert monday == WEEK_START and sunday == WEEK_END
    # 一周内的任意一天（含周日当天）都落在同一周
    assert report.week_bounds(date(2024, 1, 7)) == (WEEK_START, WEEK_END)
    # 周界本身是周一/周日
    assert date.fromisoformat(monday).weekday() == 0
    assert date.fromisoformat(sunday).weekday() == 6


def test_collect_week_stats_splits_review_grades(fresh_db):
    in_week = _make_card("2024-01-03", "周内卡")
    _make_card("2024-01-10", "下周卡")  # 范围外
    _make_feedback(in_week, "up", "2024-01-03 10:00:00")
    _make_feedback(in_week, "review:remember", "2024-01-04 10:00:00")
    _make_feedback(in_week, "review:forgot", "2024-01-05 10:00:00")

    stats = report.collect_week_stats(WEEK_START, WEEK_END)

    assert len(stats["cards"]) == 1
    assert stats["cards"][0]["title"] == "周内卡"
    assert stats["feedback"]["up"] == 1
    # review:* 单独拆出，便于周报点评复习情况
    assert stats["review_grades"] == {"remember": 1, "forgot": 1}
    assert stats["top_tags"]  # 默认播种的标签在
    assert stats["sources"]


def test_snapshot_tags_upserts_per_week(fresh_db):
    report.snapshot_tags(WEEK_START)
    first = report.get_snapshots(WEEK_START)
    assert first  # 播种标签已快照

    # 改权重后再次快照：同周覆盖，不重复
    with get_db() as conn:
        conn.execute("UPDATE tags SET weight = 9.9 WHERE name = '网络安全'")
        conn.commit()
    report.snapshot_tags(WEEK_START)
    second = report.get_snapshots(WEEK_START)
    assert second["网络安全"] == pytest.approx(9.9)
    assert len(first) == len(second)


def test_interest_profile_with_and_without_history(fresh_db):
    # 无上周快照：prev 为 None
    profile = report.interest_profile()
    assert profile and all(t["prev"] is None for t in profile)

    # 给"上周"造一份快照，本周就能算出漂移
    current_week, _ = report.week_bounds()
    prev_week = report.prev_week_start(current_week)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO tag_snapshots (tag, weight, week_start) VALUES ('网络安全', 0.5, ?)",
            (prev_week,),
        )
        conn.commit()
    profile = report.interest_profile()
    sec = next(t for t in profile if t["name"] == "网络安全")
    assert sec["prev"] == pytest.approx(0.5)


def test_report_due_now_logic(fresh_db):
    # 周日晚 21 点前：不到时候
    assert not report.report_due_now(datetime(2024, 1, 7, 20, 59))
    # 周日晚 21 点后且无本周报告：该补跑
    assert report.report_due_now(datetime(2024, 1, 7, 21, 0, 1))
    # 本周报告已存在：不用补跑
    with get_db() as conn:
        conn.execute(
            "INSERT INTO reports (week_start, week_end, content) VALUES (?, ?, '已生成')",
            (WEEK_START, WEEK_END),
        )
        conn.commit()
    assert not report.report_due_now(datetime(2024, 1, 7, 21, 0, 1))


async def test_generate_report_stores_content_and_snapshots(fresh_db, monkeypatch):
    async def fake_llm(messages, **kwargs):
        return "## 本周概览\n学了点东西。"

    monkeypatch.setattr(report, "chat_completion", fake_llm)
    _make_card("2024-01-03", "周内卡")

    report_id = await report.generate_report(WEEK_START)

    saved = report.get_report(report_id)
    assert saved["week_start"] == WEEK_START
    assert "本周概览" in saved["content"]
    assert report.get_snapshots(WEEK_START)  # 快照同步落库
    assert report.latest_report()["id"] == report_id

    # 重新生成同一周：覆盖而不是堆叠
    report_id2 = await report.generate_report(WEEK_START)
    assert report_id2 == report_id
    assert len(report.list_reports()) == 1


async def test_generate_report_raises_on_empty_llm(fresh_db, monkeypatch):
    async def empty_llm(messages, **kwargs):
        return "   "

    monkeypatch.setattr(report, "chat_completion", empty_llm)

    with pytest.raises(RuntimeError):
        await report.generate_report(WEEK_START)
    assert report.GENERATE_STATUS["state"] == "error"
    assert not report.has_report(WEEK_START)
