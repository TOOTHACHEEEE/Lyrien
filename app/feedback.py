"""反馈闭环：👍/👎/💡 更新标签权重与信源信誉（plan.md 4.3）。"""

from datetime import date, timedelta

from app.db import get_db

ACTIONS = ("up", "down", "mastered")

TAG_DELTA = {"up": 0.1, "down": -0.15}
SOURCE_DELTA = {"up": 0.05, "down": -0.1}
CARD_STATUS = {"up": "liked", "down": "disliked", "mastered": "mastered"}

MIN_TAG_WEIGHT = 0.0
MIN_SOURCE_WEIGHT = 0.1

# 简化 SM-2 初始值：已掌握的卡片 1 天后第一次复习
SM2_INITIAL_EASE = 2.5
SM2_INITIAL_INTERVAL = 1


def _split_tags(tags: str | None) -> list[str]:
    if not tags:
        return []
    return [t.strip() for t in tags.split(",") if t.strip()]


def apply_feedback(card_id: int, action: str) -> bool:
    """应用一次反馈。返回卡片是否存在（action 合法性由路由层校验）。

    - up：卡片标签 weight += 0.1，信源 weight += 0.05
    - down：卡片标签 weight -= 0.15，信源 weight -= 0.1
    - mastered：标签不动，卡片进入复习队列（SM-2：明天第一次复习）
    """
    if action not in ACTIONS:
        raise ValueError(f"非法反馈动作: {action}")

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT c.id, c.tags, i.source_id
            FROM cards c JOIN items i ON c.item_id = i.id
            WHERE c.id = ?
            """,
            (card_id,),
        ).fetchone()
        if not row:
            return False

        conn.execute(
            "INSERT INTO feedback (card_id, action) VALUES (?, ?)", (card_id, action)
        )

        if action in TAG_DELTA:
            for name in _split_tags(row["tags"]):
                conn.execute(
                    "INSERT INTO tags (name, weight) VALUES (?, 0.0) ON CONFLICT(name) DO NOTHING",
                    (name,),
                )
                conn.execute(
                    """
                    UPDATE tags
                    SET weight = MAX(?, weight + ?), updated_at = CURRENT_TIMESTAMP
                    WHERE name = ?
                    """,
                    (MIN_TAG_WEIGHT, TAG_DELTA[action], name),
                )
            conn.execute(
                "UPDATE sources SET weight = MAX(?, weight + ?) WHERE id = ?",
                (MIN_SOURCE_WEIGHT, SOURCE_DELTA[action], row["source_id"]),
            )

        if action == "mastered":
            conn.execute(
                """
                UPDATE cards
                SET status = 'mastered', ease = ?, interval = ?, due_date = ?
                WHERE id = ?
                """,
                (
                    SM2_INITIAL_EASE,
                    SM2_INITIAL_INTERVAL,
                    (date.today() + timedelta(days=SM2_INITIAL_INTERVAL)).isoformat(),
                    card_id,
                ),
            )
        else:
            conn.execute(
                "UPDATE cards SET status = ? WHERE id = ?",
                (CARD_STATUS[action], card_id),
            )

        conn.commit()
    return True


def weekly_tag_decay() -> None:
    """每周衰减 + 归一化：不互动的兴趣自然降温（plan.md 4.3）。"""
    with get_db() as conn:
        conn.execute("UPDATE tags SET weight = weight * 0.9")
        row = conn.execute("SELECT MAX(weight) AS m FROM tags").fetchone()
        max_w = row["m"] if row and row["m"] else 0
        if max_w > 0:
            conn.execute("UPDATE tags SET weight = weight / ?", (max_w,))
        conn.commit()
    print("[feedback] 兴趣标签周衰减完成")
