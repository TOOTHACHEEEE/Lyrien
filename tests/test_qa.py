"""知识库问答（qa.py）离线单测：召回排序与上下文拼装截断。"""

import pytest

from app.config import settings
from app.db import init_db
from app.notes import save_note
from app.qa import build_context, retrieve_notes


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """指向临时数据库，避免污染真实 data/lyrien.db。"""
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path}/test.db")
    init_db()
    yield tmp_path / "test.db"


def test_retrieve_notes_returns_full_content_in_rank_order(fresh_db):
    save_note("贝叶斯入门", "先验分布与后验分布的故事")
    save_note(" unrelated", "完全无关的内容")
    save_note("贝叶斯进阶", "共轭先验让贝叶斯计算更优雅")

    notes = retrieve_notes("贝叶斯")

    assert len(notes) == 2
    assert all("贝叶斯" in n["title"] for n in notes)
    # 带全文，供 LLM 阅读
    assert "共轭先验" in notes[1]["content"] or "共轭先验" in notes[0]["content"]


def test_retrieve_notes_empty_when_no_match(fresh_db):
    save_note("唯一的笔记", "内容")
    assert retrieve_notes("不存在的概念") == []


def test_build_context_numbers_blocks():
    notes = [
        {"id": 1, "title": "笔记一", "content": "内容一"},
        {"id": 2, "title": "笔记二", "content": "内容二"},
    ]
    ctx = build_context(notes)
    assert "【笔记 1】《笔记一》" in ctx
    assert "【笔记 2】《笔记二》" in ctx


def test_build_context_truncates_when_too_long():
    notes = [
        {"id": 1, "title": "短笔记", "content": "短内容"},
        {"id": 2, "title": "长笔记", "content": "长" * 5000},
        {"id": 3, "title": "塞不下的", "content": "内容三"},
    ]
    ctx = build_context(notes, max_chars=300)

    assert "【笔记 1】" in ctx
    # 第三篇塞不进 300 字预算，被丢弃
    assert "【笔记 3】" not in ctx
