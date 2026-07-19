"""手动投喂（feed.py）离线单测：去重、全文落库、状态流转（网络与 LLM 全部打桩）。"""

import pytest

import app.feed as feed
from app.config import settings
from app.db import get_db, init_db
from app.pipeline.fetch import url_hash

TEST_URL = "https://example.com/great-article"


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """指向临时数据库，避免污染真实 data/lyrien.db。"""
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path}/test.db")
    init_db()
    yield tmp_path / "test.db"


@pytest.fixture()
def fake_article(monkeypatch):
    """打桩 extract_article，避免真实网络请求。"""
    calls = {"n": 0}

    def _fake(url: str):
        calls["n"] += 1
        return {"title": "好文章", "fulltext": "这是正文内容" * 50}

    monkeypatch.setattr(feed, "extract_article", _fake)
    return calls


def _get_item(h: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT i.id, i.title, i.fulltext, s.type AS source_type
            FROM items i JOIN sources s ON i.source_id = s.id
            WHERE i.url_hash = ?
            """,
            (h,),
        ).fetchone()
    return dict(row) if row else None


def test_prepare_feed_item_fetches_and_stores(fresh_db, fake_article):
    item_id, error = feed.prepare_feed_item(TEST_URL)

    assert error is None and item_id is not None
    item = _get_item(url_hash(TEST_URL))
    assert item["title"] == "好文章"
    assert item["source_type"] == "manual"
    assert "正文内容" in item["fulltext"]


def test_prepare_feed_item_dedupes_without_refetch(fresh_db, fake_article):
    first_id, _ = feed.prepare_feed_item(TEST_URL)
    second_id, _ = feed.prepare_feed_item(TEST_URL)

    assert first_id == second_id
    assert fake_article["n"] == 1  # 第二次命中已有全文，不再抓取


def test_prepare_feed_item_fetch_failure(fresh_db, monkeypatch):
    monkeypatch.setattr(feed, "extract_article", lambda url: {"title": None, "fulltext": None})
    item_id, error = feed.prepare_feed_item(TEST_URL)

    assert item_id is None
    assert "抓不到正文" in error


async def test_feed_url_existing_card_returns_done(fresh_db, fake_article, monkeypatch):
    # 先投喂一次生成卡片
    monkeypatch.setattr(feed, "generate_card", _fake_card_json)
    result1 = await feed.feed_url(TEST_URL)
    assert result1["state"] == "done" and result1["card_id"]

    # 再次投喂同一 URL：直接命中已有卡片，不重复生成
    result2 = await feed.feed_url(TEST_URL)
    assert result2["state"] == "done"
    assert result2["card_id"] == result1["card_id"]
    assert "已经在你的卡片里" in result2["message"]


async def test_feed_url_full_flow_generates_card(fresh_db, fake_article, monkeypatch):
    monkeypatch.setattr(feed, "generate_card", _fake_card_json)

    result = await feed.feed_url(TEST_URL)

    assert result["state"] == "done"
    assert result["message"] == "卡片已生成"
    with get_db() as conn:
        card = conn.execute(
            "SELECT title, summary_json FROM cards WHERE id = ?", (result["card_id"],)
        ).fetchone()
    assert card is not None
    assert "核心" in card["summary_json"]


async def _fake_card_json(item):
    return {"core_concept": "核心概念", "why_learn": "值得学", "guide_question": "想一想"}
