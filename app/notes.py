"""知识库笔记：深挖教程与手动笔记的保存、FTS5 全文搜索与新旧知识关联。

FTS5 自带的 unicode61 分词器会把连续中文当成一个词条，无法检索。
做法是在入库前把 CJK 字符逐字用空格隔开（字级索引，见 db.segment_cjk），
查询时对搜索词做同样处理 —— 零依赖地获得可用的中文全文搜索。
"""

import html
import re
from typing import Any

from app.db import get_db, segment_cjk

# 展示用：去掉分词在相邻中文字符之间插入的空格
_CJK_SPACE_RE = re.compile(r"(?<=[㐀-䶿一-鿿豈-﫿]) (?=[㐀-䶿一-鿿豈-﫿])")


def _unsegment(text: str) -> str:
    """去掉相邻中文字符之间的空格，用于搜索结果展示。"""
    return _CJK_SPACE_RE.sub("", text)


def _build_match_query(query: str) -> str:
    """把用户输入转成安全的 FTS5 AND 查询：逐词加引号，CJK 先分字。"""
    tokens = segment_cjk(query).split()
    return " ".join(f'"{t}"' for t in tokens)


def normalize_tags(tags: str | None) -> str | None:
    """"网安,  C++ ，统计学" → "网安, C++, 统计学"；空输入归一为 None。"""
    if not tags:
        return None
    parts = [t.strip() for t in tags.replace("，", ",").split(",") if t.strip()]
    return ", ".join(parts) if parts else None


def save_note(
    title: str,
    content: str,
    card_id: int | None = None,
    tags: str | None = None,
) -> int:
    """保存笔记并同步全文索引，返回 note_id。"""
    tags = normalize_tags(tags)
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO notes (title, content, card_id, tags) VALUES (?, ?, ?, ?)",
            (title, content, card_id, tags),
        )
        note_id = cur.lastrowid
        conn.execute(
            "INSERT INTO notes_fts (rowid, title, content, tags) VALUES (?, ?, ?, ?)",
            (note_id, segment_cjk(title), segment_cjk(content), segment_cjk(tags or "")),
        )
        conn.commit()
    return note_id


def update_note(
    note_id: int,
    title: str,
    content: str,
    tags: str | None = None,
) -> bool:
    """更新笔记并重同步全文索引；笔记不存在返回 False。"""
    tags = normalize_tags(tags)
    with get_db() as conn:
        cur = conn.execute(
            """
            UPDATE notes
            SET title = ?, content = ?, tags = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (title, content, tags, note_id),
        )
        if cur.rowcount == 0:
            return False
        conn.execute("DELETE FROM notes_fts WHERE rowid = ?", (note_id,))
        conn.execute(
            "INSERT INTO notes_fts (rowid, title, content, tags) VALUES (?, ?, ?, ?)",
            (note_id, segment_cjk(title), segment_cjk(content), segment_cjk(tags or "")),
        )
        conn.commit()
    return True


def note_for_card(card_id: int) -> dict[str, Any] | None:
    """某张卡片是否已深挖过（深挖是幂等的，不重复生成）。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, title, created_at FROM notes WHERE card_id = ? ORDER BY id DESC LIMIT 1",
            (card_id,),
        ).fetchone()
    return dict(row) if row else None


def get_note(note_id: int) -> dict[str, Any] | None:
    """读取单条笔记，附带其来源卡片信息。tags 优先取笔记自身的标签。"""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT n.id, n.title, n.content, n.card_id, n.created_at, n.updated_at,
                   COALESCE(n.tags, c.tags) AS tags,
                   c.title AS card_title, i.url AS source_url
            FROM notes n
            LEFT JOIN cards c ON n.card_id = c.id
            LEFT JOIN items i ON c.item_id = i.id
            WHERE n.id = ?
            """,
            (note_id,),
        ).fetchone()
    return dict(row) if row else None


def list_notes(limit: int = 50) -> list[dict[str, Any]]:
    """最近的笔记列表。"""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT n.id, n.title, substr(n.content, 1, 160) AS excerpt,
                   n.created_at, n.card_id, COALESCE(n.tags, c.tags) AS tags
            FROM notes n
            LEFT JOIN cards c ON n.card_id = c.id
            ORDER BY n.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def search_notes(query: str, limit: int = 30) -> list[dict[str, Any]]:
    """FTS5 全文搜索（标题/正文/标签）；查询异常或 FTS 不可用时降级为 LIKE。"""
    match = _build_match_query(query)
    if not match:
        return []
    with get_db() as conn:
        try:
            rows = conn.execute(
                """
                SELECT n.id, n.title, n.created_at, n.card_id,
                       COALESCE(n.tags, c.tags) AS tags,
                       snippet(notes_fts, 1, '<mark>', '</mark>', '…', 12) AS excerpt
                FROM notes_fts
                JOIN notes n ON n.id = notes_fts.rowid
                LEFT JOIN cards c ON n.card_id = c.id
                WHERE notes_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (match, limit),
            ).fetchall()
        except Exception:
            like = f"%{query}%"
            rows = conn.execute(
                """
                SELECT n.id, n.title, substr(n.content, 1, 160) AS excerpt,
                       n.created_at, n.card_id, COALESCE(n.tags, c.tags) AS tags
                FROM notes n
                LEFT JOIN cards c ON n.card_id = c.id
                WHERE n.title LIKE ? OR n.content LIKE ? OR n.tags LIKE ?
                ORDER BY n.id DESC
                LIMIT ?
                """,
                (like, like, like, limit),
            ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["excerpt"] = _safe_excerpt(item.get("excerpt") or "")
        results.append(item)
    return results


def _safe_excerpt(excerpt: str) -> str:
    """搜索摘要展示：还原中文空格、HTML 转义，只放行 FTS 的 <mark> 高亮标签。"""
    safe = html.escape(_unsegment(excerpt), quote=False)
    return safe.replace("&lt;mark&gt;", "<mark>").replace("&lt;/mark&gt;", "</mark>")


def _split_tags(tags: str | None) -> set[str]:
    if not tags:
        return set()
    return {t.strip() for t in tags.split(",") if t.strip()}


def related_notes(note_id: int, limit: int = 5) -> list[dict[str, Any]]:
    """新旧知识关联：共享标签（笔记自身标签 ∪ 来源卡片标签）的其他笔记。

    手动笔记没有来源卡片，靠自己的标签参与关联 —— 与深挖笔记地位相同。
    """
    with get_db() as conn:
        base = conn.execute(
            """
            SELECT n.tags AS note_tags, c.tags AS card_tags
            FROM notes n LEFT JOIN cards c ON n.card_id = c.id
            WHERE n.id = ?
            """,
            (note_id,),
        ).fetchone()
        if not base:
            return []
        base_tags = _split_tags(base["note_tags"]) | _split_tags(base["card_tags"])
        if not base_tags:
            return []

        rows = conn.execute(
            """
            SELECT n.id, n.title, n.created_at,
                   n.tags AS note_tags, c.tags AS card_tags
            FROM notes n LEFT JOIN cards c ON n.card_id = c.id
            WHERE n.id != ?
            ORDER BY n.id DESC
            """,
            (note_id,),
        ).fetchall()

    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        cand_tags = _split_tags(row["note_tags"]) | _split_tags(row["card_tags"])
        shared = base_tags & cand_tags
        if shared:
            scored.append((len(shared), {"id": row["id"], "title": row["title"],
                                         "created_at": row["created_at"],
                                         "shared_tags": sorted(shared)}))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[:limit]]


def note_count() -> int:
    with get_db() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()
    return row["n"]


def random_old_note(min_age_days: int = 7) -> dict[str, Any] | None:
    """知识考古：随机翻出一篇 N 天前的旧笔记，问一句"还记得吗"。"""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT n.id, n.title, substr(n.content, 1, 200) AS excerpt, n.created_at
            FROM notes n
            WHERE n.created_at <= datetime('now', ?)
            ORDER BY RANDOM() LIMIT 1
            """,
            (f"-{min_age_days} days",),
        ).fetchone()
    return dict(row) if row else None
