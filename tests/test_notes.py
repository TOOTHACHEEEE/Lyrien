"""知识库笔记（notes.py）离线单测：保存、FTS5 中文检索、关联查询、更新与迁移。"""

import sqlite3

import pytest

from app.config import settings
from app.db import get_db, init_db, segment_cjk
from app.notes import (
    get_note,
    list_notes,
    normalize_tags,
    note_for_card,
    related_notes,
    save_note,
    search_notes,
    update_note,
)


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """指向临时数据库，避免污染真实 data/lyrien.db。"""
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path}/test.db")
    init_db()
    yield tmp_path / "test.db"


def _make_card(tags: str) -> int:
    """造一个 item + card，返回 card_id。"""
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO items (source_id, title, url, summary, url_hash)
            VALUES (1, '测试文章', ?, '摘要', ?)
            """,
            (f"https://example.com/{tags}", f"hash-{tags}"),
        )
        item_id = cur.lastrowid
        cur = conn.execute(
            """
            INSERT INTO cards (date, item_id, title, summary_json, tags)
            VALUES ('2026-07-19', ?, '测试卡片', '{}', ?)
            """,
            (item_id, tags),
        )
        conn.commit()
        return cur.lastrowid


def test_segment_cjk_inserts_spaces_between_chars():
    assert segment_cjk("网络安全") == "网 络 安 全 "
    # ASCII 词保持完整
    assert segment_cjk("Linux 内核") == "Linux 内 核 "


def test_save_note_and_lookup_by_card(fresh_db):
    card_id = _make_card("网络安全")
    note_id = save_note("SQL 注入深入解析", "正文内容", card_id=card_id)

    assert note_id > 0
    note = get_note(note_id)
    assert note["title"] == "SQL 注入深入解析"
    assert note["card_id"] == card_id

    existing = note_for_card(card_id)
    assert existing is not None and existing["id"] == note_id
    assert note_for_card(9999) is None


def test_search_notes_finds_chinese_fulltext(fresh_db):
    save_note("内存分配机制", "slab 分配器通过对象缓存减少碎片", card_id=_make_card("Linux 内核"))
    save_note("贝叶斯推断入门", "先验与后验的故事", card_id=_make_card("统计学"))

    results = search_notes("分配器")
    assert len(results) == 1
    assert results[0]["title"] == "内存分配机制"
    assert "<mark>" in results[0]["excerpt"]

    # 标题也能命中；无结果时返回空列表
    assert search_notes("贝叶斯")[0]["title"] == "贝叶斯推断入门"
    assert search_notes("量子引力") == []


def test_search_notes_mixed_cjk_ascii_query(fresh_db):
    save_note("Linux 的 slab 防护", "内核加固", card_id=_make_card("Linux 内核"))

    results = search_notes("Linux 防护")
    assert len(results) == 1
    assert results[0]["title"] == "Linux 的 slab 防护"


def test_list_notes_returns_recent_first(fresh_db):
    save_note("第一篇", "内容一")
    save_note("第二篇", "内容二")
    notes = list_notes()
    assert [n["title"] for n in notes] == ["第二篇", "第一篇"]


def test_related_notes_by_shared_tags(fresh_db):
    card_a = _make_card("网络安全, Linux 内核")
    card_b = _make_card("网络安全")
    card_c = _make_card("统计学")
    note_a = save_note("笔记A", "内容", card_id=card_a)
    note_b = save_note("笔记B", "内容", card_id=card_b)
    save_note("笔记C", "内容", card_id=card_c)

    related = related_notes(note_a)
    assert len(related) == 1
    assert related[0]["id"] == note_b
    assert related[0]["shared_tags"] == ["网络安全"]

    # 关联是双向的：B 也能通过"网络安全"找回 A
    related_b = related_notes(note_b)
    assert [r["id"] for r in related_b] == [note_a]

    # 无卡片 / 无标签时不报错
    orphan = save_note("无源笔记", "内容")
    assert related_notes(orphan) == []


def test_normalize_tags():
    assert normalize_tags("网安,  C++ ，统计学") == "网安, C++, 统计学"
    assert normalize_tags("  ") is None
    assert normalize_tags(None) is None


def test_save_note_with_tags_searchable_by_tag(fresh_db):
    save_note("我的随笔", "正文没有关键词", tags="网络安全, 随笔")
    note = get_note(search_notes("网络安全")[0]["id"])
    assert note["title"] == "我的随笔"
    assert note["tags"] == "网络安全, 随笔"


def test_update_note_resyncs_fts(fresh_db):
    note_id = save_note("旧标题", "旧正文讲防火墙", tags="旧标签")

    assert update_note(note_id, "新标题", "新正文讲加密", tags="密码学") is True

    note = get_note(note_id)
    assert note["title"] == "新标题"
    assert note["tags"] == "密码学"
    assert note["updated_at"] is not None

    # 旧内容搜不到，新内容搜得到（FTS 已重同步）
    assert search_notes("防火墙") == []
    assert search_notes("加密")[0]["id"] == note_id
    assert search_notes("旧标签") == []
    assert search_notes("密码学")[0]["id"] == note_id

    assert update_note(9999, "x", "y") is False


def test_related_notes_manual_note_by_own_tags(fresh_db):
    # 手动笔记靠自身标签与深挖笔记（卡片标签）互相关联
    card = _make_card("网络安全")
    dig_note = save_note("深挖笔记", "内容", card_id=card)
    manual = save_note("我的手记", "内容", tags="网络安全")
    unrelated = save_note("无关手记", "内容", tags="烹饪")

    related = related_notes(manual)
    assert [r["id"] for r in related] == [dig_note]
    assert related[0]["shared_tags"] == ["网络安全"]
    # 双向可达
    assert [r["id"] for r in related_notes(dig_note)] == [manual]
    assert related_notes(unrelated) == []


def test_migration_from_legacy_schema(tmp_path, monkeypatch):
    """旧两列表结构的库：init_db 应加列、重建 FTS 且旧数据仍可搜到。"""
    db_file = tmp_path / "legacy.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_file}")

    # 手工搭一个 Phase 3 时代的旧库
    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            card_id INTEGER,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute("CREATE VIRTUAL TABLE notes_fts USING fts5(title, content)")
    conn.execute("INSERT INTO notes (title, content) VALUES ('遗留笔记', '讲内存屏障')")
    conn.execute(
        "INSERT INTO notes_fts (rowid, title, content) VALUES (1, '遗 留 笔 记 ', '讲 内 存 屏 障 ')"
    )
    conn.commit()
    conn.close()

    init_db()  # 触发迁移

    with get_db() as conn:
        note_cols = {row[1] for row in conn.execute("PRAGMA table_info(notes)")}
        fts_cols = {row[1] for row in conn.execute("PRAGMA table_info(notes_fts)")}
    assert {"tags", "updated_at"} <= note_cols
    assert {"title", "content", "tags"} <= fts_cols

    # 旧笔记迁移后仍可被中文检索命中
    results = search_notes("内存屏障")
    assert len(results) == 1
    assert results[0]["title"] == "遗留笔记"
    # 迁移后能正常更新
    assert update_note(results[0]["id"], "遗留笔记", "改讲原子操作") is True
    assert search_notes("原子操作")
