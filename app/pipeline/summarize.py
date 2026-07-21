"""LLM 精选与卡片生成（两阶段过滤的第二阶段）。"""

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from app.config import settings
from app.db import get_db
from app.llm import chat_completion_json
from app.pipeline.fetch import ensure_fulltext
from app.pipeline.filter import prescreen

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def get_top_tags(limit: int = 8) -> list[str]:
    """获取权重最高的兴趣标签。"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT name FROM tags ORDER BY weight DESC, updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [row["name"] for row in rows]


def get_candidate_items(limit: int = 100) -> list[dict[str, Any]]:
    """获取尚未生成卡片的候选条目，供初筛使用。"""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT i.id, i.title, i.url, i.summary, i.fulltext,
                   s.name AS source_name, s.weight AS source_weight
            FROM items i
            JOIN sources s ON i.source_id = s.id
            WHERE NOT EXISTS (
                SELECT 1 FROM cards c WHERE c.item_id = i.id
            )
            ORDER BY i.fetched_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


async def select_items(
    main_pool: list[dict[str, Any]],
    explore_pool: list[dict[str, Any]],
    k: int | None = None,
) -> list[dict[str, Any]]:
    """调用 LLM 从兴趣池精选 k 条 + 从探索池选 1 条探索卡。"""
    combined = main_pool + explore_pool
    if not combined:
        return []

    k = k or settings.cards_per_day
    top_tags = get_top_tags()
    prompt = load_prompt("select").format(
        top_tags=", ".join(top_tags) if top_tags else "C++、网络安全、统计学",
        n=len(combined),
        k=k,
    )

    # 构造带全局索引的输入，兴趣池与探索池分段标注
    def fmt(idx: int, item: dict[str, Any]) -> str:
        snippet = (item["summary"] or "")[:800].replace("\n", " ")
        return f"[{idx}] 标题: {item['title']}\n来源: {item['source_name']}\n摘要: {snippet}"

    sections = ["【兴趣池】"]
    sections.extend(fmt(i, item) for i, item in enumerate(main_pool))
    if explore_pool:
        sections.append("\n【探索池】（与上述兴趣无关，用于防止信息茧房）")
        offset = len(main_pool)
        sections.extend(fmt(offset + i, item) for i, item in enumerate(explore_pool))

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "\n\n".join(sections)},
    ]

    try:
        result = await chat_completion_json(messages, temperature=0.3)
        if isinstance(result, dict):
            # 兼容模型把数组包进 choices/items/results 等字段的情况
            for key in ("choices", "items", "results", "selected", "data"):
                if key in result and isinstance(result[key], list):
                    result = result[key]
                    break
            else:
                print(f"[select] LLM 返回非数组结构: {result}")
                return []
        if not isinstance(result, list):
            print(f"[select] LLM 返回非数组: {result}")
            return []
    except Exception as exc:
        print(f"[select] 精选失败: {exc}")
        return []

    selected: list[dict[str, Any]] = []
    normal_count = 0
    explore_count = 0
    for r in result:
        idx = r.get("index")
        if not isinstance(idx, int) or not 0 <= idx < len(combined):
            continue
        # 探索卡身份以所在池为准，同时兼容 LLM 的 is_explore 标记
        is_explore = idx >= len(main_pool) or bool(r.get("is_explore"))
        if is_explore:
            if explore_count >= 1:
                continue  # 每天最多 1 张探索卡
            explore_count += 1
        else:
            if normal_count >= k:
                continue
            normal_count += 1
        selected.append({**combined[idx], "is_explore": is_explore, "reason": r.get("reason", "")})

    print(f"[select] 精选 {normal_count} 条 + 探索卡 {explore_count} 张")
    return selected


async def generate_card(item: dict[str, Any]) -> dict[str, Any] | None:
    """为单条 item 生成结构化学习卡片。"""
    body = item["fulltext"] or item["summary"] or ""
    if not body.strip():
        print(f"[card] 跳过无正文条目: {item['url']}")
        return None

    prompt = load_prompt("card").format(
        title=item["title"],
        url=item["url"],
        fulltext=body[:6000],  # 控制 token
    )
    messages = [
        {"role": "system", "content": "你是一位严谨的技术导师。"},
        {"role": "user", "content": prompt},
    ]

    try:
        result = await chat_completion_json(messages, temperature=0.5)
        if not isinstance(result, dict):
            print(f"[card] LLM 返回非对象: {result}")
            return None
        return result
    except Exception as exc:
        print(f"[card] 生成失败 {item['url']}: {exc}")
        return None


def _clean_text(obj: Any) -> Any:
    """递归清理文本中的 em-dash / en-dash，替换为短横线或标点。"""
    if isinstance(obj, str):
        return obj.replace("—", " - ").replace("–", "-")
    if isinstance(obj, list):
        return [_clean_text(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _clean_text(v) for k, v in obj.items()}
    return obj


def save_card(item: dict[str, Any], card_json: dict[str, Any], is_explore: bool = False) -> int:
    """将生成的卡片写入 cards 表。"""
    today = date.today().isoformat()
    card_json = _clean_text(card_json)
    item = {**item, "title": _clean_text(item.get("title", ""))}
    related = card_json.get("related_knowledge", [])
    if isinstance(related, list):
        tags = ", ".join(str(t) for t in related)
    elif isinstance(related, str):
        tags = related
    else:
        tags = str(related)
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO cards (date, item_id, title, summary_json, tags, is_explore, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                today,
                item["id"],
                item["title"],
                json.dumps(card_json, ensure_ascii=False),
                tags,
                1 if is_explore else 0,
                "new",
            ),
        )
        conn.commit()
    return cur.lastrowid


async def generate_cards(k: int | None = None) -> list[int]:
    """生成今日卡片，返回新插入的卡片 ID 列表。

    流程：初筛（零 LLM 成本）→ LLM 精选 → 对入选条目抓全文 → 逐条生成卡片。
    """
    items = get_candidate_items()
    if not items:
        print("[summarize] 没有候选条目")
        return []

    main_pool, explore_pool = prescreen(items)
    selected = await select_items(main_pool, explore_pool, k=k)

    card_ids: list[int] = []
    for item in selected:
        # 精选后才抓全文；失败则保留摘要降级
        fulltext = ensure_fulltext(item["id"])
        if fulltext:
            item["fulltext"] = fulltext
        card_json = await generate_card(item)
        if card_json:
            try:
                cid = save_card(item, card_json, is_explore=item.get("is_explore", False))
            except sqlite3.IntegrityError:
                # 条目已被并发运行生成过卡片，跳过而不拖垮整批
                print(f"[summarize] 条目 {item['id']} 已有卡片，跳过重复保存")
                continue
            card_ids.append(cid)

    print(f"[summarize] 生成 {len(card_ids)} 张卡片")
    return card_ids
