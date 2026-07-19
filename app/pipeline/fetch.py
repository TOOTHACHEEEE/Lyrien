"""RSS/API 抓取 + 正文提取。"""

import hashlib
import html
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import feedparser
import trafilatura

from app.config import settings
from app.db import get_db


def url_hash(url: str) -> str:
    """URL 标准化后的 SHA256 哈希。"""
    parsed = urlparse(url)
    # 忽略协议差异与尾部斜杠
    normalized = f"{parsed.netloc}{parsed.path.rstrip('/')}/"
    return hashlib.sha256(normalized.lower().encode()).hexdigest()[:32]


def _parse_published(entry: dict[str, Any]) -> datetime | None:
    """尝试从 feed entry 解析发布时间。"""
    if "published_parsed" in entry and entry["published_parsed"]:
        return datetime(*entry["published_parsed"][:6], tzinfo=UTC)
    if "updated_parsed" in entry and entry["updated_parsed"]:
        return datetime(*entry["updated_parsed"][:6], tzinfo=UTC)
    return None


def _extract_summary(entry: dict[str, Any]) -> str:
    """从 entry 中提取摘要，清理 HTML。"""
    raw = entry.get("summary", "") or entry.get("description", "")
    # feedparser 有时会给出纯文本，有时会给出 HTML
    if raw.startswith("<") and ">" in raw:
        # 简单 unescape，不引入 bs4 依赖
        return html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return html.unescape(raw)


def fetch_source(source: dict[str, Any]) -> list[dict[str, Any]]:
    """抓取单个 RSS 信源，返回近 N 小时的条目。"""
    try:
        feed = feedparser.parse(source["url"])
    except Exception as exc:
        print(f"[fetch] 信源解析失败 {source['name']}: {exc}")
        return []

    if feed.get("bozo_exception") and not feed.entries:
        print(f"[fetch] 信源异常 {source['name']}: {feed.bozo_exception}")
        return []

    cutoff = datetime.now(UTC) - timedelta(hours=settings.fetch_window_hours)
    results: list[dict[str, Any]] = []

    for entry in feed.entries:
        link = entry.get("link", "").strip()
        if not link:
            continue

        published = _parse_published(entry)
        if published and published < cutoff:
            continue

        title = html.unescape(entry.get("title", "无标题")).strip()
        summary = _extract_summary(entry).strip()
        results.append(
            {
                "source_id": source["id"],
                "title": title or "无标题",
                "url": link,
                "summary": summary,
                "published": published,
            }
        )

    print(f"[fetch] {source['name']}: {len(results)} 条新条目")
    return results


def extract_fulltext(url: str) -> str | None:
    """用 trafilatura 提取正文，失败返回 None。"""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        return text.strip() if text else None
    except Exception as exc:
        print(f"[fetch] 全文提取失败 {url}: {exc}")
        return None


def extract_article(url: str) -> dict[str, str | None]:
    """提取正文 + 页面标题（手动投喂用），失败返回两个 None。"""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return {"title": None, "fulltext": None}
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        title = None
        metadata = trafilatura.extract_metadata(downloaded)
        if metadata and metadata.title:
            title = metadata.title.strip()
        return {"title": title or None, "fulltext": text.strip() if text else None}
    except Exception as exc:
        print(f"[fetch] 文章提取失败 {url}: {exc}")
        return {"title": None, "fulltext": None}


def ensure_fulltext(item_id: int) -> str | None:
    """对精选出的条目提取全文并写回 items 表；已有全文则直接返回。

    全文抓取只发生在精选之后（plan 4.1 步骤⑤），避免对全部候选逐条请求。
    失败返回 None，调用方降级用摘要。
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT url, fulltext FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if not row:
            return None
        if row["fulltext"]:
            return row["fulltext"]

        fulltext = extract_fulltext(row["url"])
        if fulltext:
            conn.execute(
                "UPDATE items SET fulltext = ? WHERE id = ?", (fulltext, item_id)
            )
            conn.commit()
        return fulltext


def save_items(items: list[dict[str, Any]]) -> list[int]:
    """将条目写入 items 表，去重；返回新插入的 item_id 列表。

    只保存标题/摘要/URL，全文延迟到精选后由 ensure_fulltext 补齐。
    """
    inserted_ids: list[int] = []
    if not items:
        return inserted_ids

    with get_db() as conn:
        for item in items:
            h = url_hash(item["url"])
            # 已存在则跳过
            existing = conn.execute(
                "SELECT id FROM items WHERE url_hash = ?", (h,)
            ).fetchone()
            if existing:
                continue

            cur = conn.execute(
                """
                INSERT INTO items (source_id, title, url, summary, url_hash)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    item["source_id"],
                    item["title"],
                    item["url"],
                    item["summary"],
                    h,
                ),
            )
            inserted_ids.append(cur.lastrowid)
        conn.commit()

    return inserted_ids


def fetch_all() -> list[int]:
    """抓取所有启用信源，去重入库，返回本次新增 item_id 列表。"""
    with get_db() as conn:
        sources = conn.execute(
            "SELECT id, name, url FROM sources WHERE enabled = 1 AND type = 'rss'"
        ).fetchall()

    all_items: list[dict[str, Any]] = []
    for source in sources:
        try:
            items = fetch_source(dict(source))
            all_items.extend(items)
        except Exception as exc:
            print(f"[fetch] 信源 {source['name']} 失败: {exc}")

    print(f"[fetch] 总计新条目: {len(all_items)}")
    return save_items(all_items)


if __name__ == "__main__":
    init_required = True
    try:
        from app.db import init_db

        init_db()
        fetch_all()
    except Exception as exc:
        print(f"[fetch] 运行失败: {exc}")
        raise
