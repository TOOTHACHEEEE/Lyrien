"""知识库问答（RAG-lite）：FTS5 召回笔记 → LLM 基于笔记内容作答。

刻意不用向量库：默认 LLM 走 DeepSeek，没有 embeddings 接口；
知识库量级（几百篇）下 FTS5 召回 + LLM 重排上下文已足够，零新增依赖。
"""

from typing import Any

from app.db import get_db
from app.notes import search_notes

MAX_CONTEXT_CHARS = 6000
RETRIEVE_K = 5


def retrieve_notes(query: str, k: int = RETRIEVE_K) -> list[dict[str, Any]]:
    """用 FTS5 召回最相关的 k 篇笔记（含全文）。"""
    hits = search_notes(query, limit=k)
    if not hits:
        return []
    ids = [h["id"] for h in hits]
    placeholders = ",".join("?" for _ in ids)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT id, title, content, created_at FROM notes WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    # 按 FTS 排名顺序重排
    order = {note_id: i for i, note_id in enumerate(ids)}
    notes = [dict(row) for row in rows]
    notes.sort(key=lambda n: order[n["id"]])
    return notes


def build_context(notes: list[dict[str, Any]], max_chars: int = MAX_CONTEXT_CHARS) -> str:
    """把召回笔记拼成带编号的上下文块，超长截断尾部笔记。"""
    blocks: list[str] = []
    used = 0
    for i, note in enumerate(notes, start=1):
        block = f"【笔记 {i}】《{note['title']}》\n{note['content']}"
        if used + len(block) > max_chars:
            remaining = max_chars - used
            if remaining < 200:  # 塞不下的整块直接放弃
                break
            block = block[:remaining] + "…"
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)
