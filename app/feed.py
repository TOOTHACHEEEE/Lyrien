"""手动投喂：粘贴任意 URL，走与每日管线相同的 全文提取 → 卡片生成 流程。

投喂是按需行为，不受 cards_per_day 每日配额限制。
单进程应用，任务状态放内存即可（重启丢失只影响"生成中"的提示，不影响数据）。
"""

import asyncio
from typing import Any

from app.db import get_db
from app.pipeline.fetch import extract_article, url_hash
from app.pipeline.summarize import generate_card, save_card

MANUAL_SOURCE_NAME = "手动投喂"

# url_hash -> {"state": pending|done|error, "message": str, "card_id": int|None}
_STATUS: dict[str, dict[str, Any]] = {}


def _manual_source_id() -> int:
    """获取内置"手动投喂"信源 id（init_db 已播种，防御性兜底）。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM sources WHERE name = ?", (MANUAL_SOURCE_NAME,)
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO sources (name, type, url, weight) VALUES (?, 'manual', 'manual://feed', 1.0)",
            (MANUAL_SOURCE_NAME,),
        )
        conn.commit()
        return cur.lastrowid


def _existing_card_id(h: str) -> int | None:
    """该 URL 是否已生成过卡片（去重）。"""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT c.id FROM items i JOIN cards c ON c.item_id = i.id
            WHERE i.url_hash = ?
            """,
            (h,),
        ).fetchone()
    return row["id"] if row else None


def prepare_feed_item(url: str) -> tuple[int | None, str | None]:
    """抓全文并写入 items 表（不含 LLM 步骤，便于单测）。

    返回 (item_id, error_message)。已存在但未生成卡片时复用旧 item。
    """
    h = url_hash(url)
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, fulltext FROM items WHERE url_hash = ?", (h,)
        ).fetchone()

    if row:
        item_id, fulltext = row["id"], row["fulltext"]
    else:
        item_id, fulltext = None, None

    title = url
    if not fulltext:
        article = extract_article(url)
        if not article["fulltext"]:
            return None, "抓不到正文，这个页面可能不支持提取"
        fulltext = article["fulltext"]
        title = article["title"] or url

    if item_id is None:
        with get_db() as conn:
            cur = conn.execute(
                """
                INSERT INTO items (source_id, title, url, summary, fulltext, url_hash)
                VALUES (?, ?, ?, '', ?, ?)
                """,
                (_manual_source_id(), title, url, fulltext, h),
            )
            item_id = cur.lastrowid
            conn.commit()
    elif not row["fulltext"]:
        with get_db() as conn:
            conn.execute("UPDATE items SET fulltext = ? WHERE id = ?", (fulltext, item_id))
            conn.commit()

    return item_id, None


async def feed_url(url: str) -> dict[str, Any]:
    """完整投喂流程：去重 → 抓全文 → LLM 生成卡片（异步任务入口）。"""
    h = url_hash(url)
    _STATUS[h] = {"state": "pending", "message": "正在抓取正文…", "card_id": None}
    try:
        existing = _existing_card_id(h)
        if existing:
            _STATUS[h] = {"state": "done", "message": "这条内容已经在你的卡片里", "card_id": existing}
            return _STATUS[h]

        item_id, error = await asyncio.to_thread(prepare_feed_item, url)
        if error or item_id is None:
            _STATUS[h] = {"state": "error", "message": error or "抓取失败", "card_id": None}
            return _STATUS[h]

        _STATUS[h]["message"] = "正文到手，正在生成卡片…"
        with get_db() as conn:
            row = conn.execute(
                "SELECT id, title, url, summary, fulltext FROM items WHERE id = ?",
                (item_id,),
            ).fetchone()
        card_json = await generate_card(dict(row))
        if not card_json:
            _STATUS[h] = {"state": "error", "message": "卡片生成失败，换个时间再试", "card_id": None}
            return _STATUS[h]

        card_id = save_card(dict(row), card_json)
        _STATUS[h] = {"state": "done", "message": "卡片已生成", "card_id": card_id}
        return _STATUS[h]
    except Exception as exc:  # 兜底：任何异常都要落到状态里，前端才不致干等
        _STATUS[h] = {"state": "error", "message": f"投喂失败: {exc}", "card_id": None}
        return _STATUS[h]


def start_feed(url: str) -> str:
    """启动后台投喂任务，返回 url_hash 供前端轮询状态。"""
    h = url_hash(url)
    # 重复提交：已有任务在跑就直接复用
    if _STATUS.get(h, {}).get("state") != "pending":
        asyncio.get_running_loop().create_task(feed_url(url))
    return h


def feed_status(h: str) -> dict[str, Any]:
    return _STATUS.get(h, {"state": "unknown", "message": "没有这个任务", "card_id": None})
