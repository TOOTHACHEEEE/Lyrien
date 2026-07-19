"""两阶段过滤的第一阶段：零 LLM 成本的规则 + 兴趣标签关键词初筛。

第二阶段（LLM 精选）在 summarize.py 中。初筛把 ~100 条候选压到
兴趣池 Top N + 探索池 Top M，控制每日 LLM 调用成本。
"""

from typing import Any

from app.config import settings
from app.db import get_db


def get_tag_weights() -> dict[str, float]:
    """读取兴趣标签权重表。"""
    with get_db() as conn:
        rows = conn.execute("SELECT name, weight FROM tags").fetchall()
    return {row["name"]: row["weight"] for row in rows}


def _item_text(item: dict[str, Any]) -> str:
    return f"{item.get('title', '')} {item.get('summary', '')}".lower()


def has_tag_match(item: dict[str, Any], tag_weights: dict[str, float]) -> bool:
    """条目标题/摘要是否命中任一兴趣标签。"""
    text = _item_text(item)
    return any(tag.lower() in text for tag in tag_weights)


def score_item(item: dict[str, Any], tag_weights: dict[str, float]) -> float:
    """对命中兴趣的候选打分：标签权重累加，乘信源权重；无摘要降权。"""
    text = _item_text(item)
    score = 0.0
    for tag, weight in tag_weights.items():
        if tag.lower() in text:
            score += weight
    score *= item.get("source_weight", 1.0)
    if not (item.get("summary") or "").strip():
        score *= 0.5
    return score


def _explore_quality(item: dict[str, Any]) -> float:
    """探索池排序依据：信源权重为主，有摘要加分。"""
    score = item.get("source_weight", 1.0)
    if (item.get("summary") or "").strip():
        score += 0.2
    return score


def prescreen(
    items: list[dict[str, Any]],
    main_size: int | None = None,
    explore_size: int | None = None,
    tag_weights: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """初筛候选条目，返回 (兴趣池, 探索池)。

    兴趣池：命中兴趣标签、得分 Top N。
    探索池：未命中任何兴趣标签的条目（来自圈外，防信息茧房），按信源质量取 Top M。
    """
    main_size = main_size or settings.prescreen_pool_size
    explore_size = explore_size or settings.explore_pool_size
    if tag_weights is None:
        tag_weights = get_tag_weights()

    matched: list[tuple[float, dict[str, Any]]] = []
    unmatched: list[dict[str, Any]] = []
    for item in items:
        if has_tag_match(item, tag_weights):
            matched.append((score_item(item, tag_weights), item))
        else:
            unmatched.append(item)

    matched.sort(key=lambda x: x[0], reverse=True)
    unmatched.sort(key=_explore_quality, reverse=True)

    main_pool = [dict(it, prescreen_score=s) for s, it in matched[:main_size]]
    explore_pool = [dict(it, prescreen_score=_explore_quality(it)) for it in unmatched[:explore_size]]
    print(f"[filter] 初筛: {len(items)} → 兴趣池 {len(main_pool)} / 探索池 {len(explore_pool)}")
    return main_pool, explore_pool
