"""简化 SM-2 间隔重复复习队列（plan.md Phase 3）。

流程：卡片点 💡 已掌握 → feedback.py 写入 ease=2.5, interval=1, due_date=明天
     → 复习页展示到期卡片 → 用户自评（记得/模糊/忘记）→ 更新三元组
间隔序列大致为 1 → 2~3 → 6~7 → 指数拉长；忘记则回到 1 天重学。
"""

from datetime import date, timedelta
from typing import Any, Literal

from app.db import get_db

Grade = Literal["remember", "fuzzy", "forgot"]
GRADES: tuple[str, ...] = ("remember", "fuzzy", "forgot")

MIN_EASE = 1.3
MAX_EASE = 2.8


def get_due_cards(today: date | None = None) -> list[dict[str, Any]]:
    """所有到期（due_date <= 今天）的复习卡片。"""
    today = today or date.today()
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.title, c.summary_json, c.tags, c.ease, c.interval, c.due_date,
                   s.name AS source_name, i.url
            FROM cards c
            JOIN items i ON c.item_id = i.id
            JOIN sources s ON i.source_id = s.id
            WHERE c.status = 'mastered' AND c.due_date IS NOT NULL AND c.due_date <= ?
            ORDER BY c.due_date, c.id
            """,
            (today.isoformat(),),
        ).fetchall()
    return [dict(row) for row in rows]


def due_count(today: date | None = None) -> int:
    """今日到期卡片数（导航角标用）。"""
    today = today or date.today()
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n FROM cards
            WHERE status = 'mastered' AND due_date IS NOT NULL AND due_date <= ?
            """,
            (today.isoformat(),),
        ).fetchone()
    return row["n"]


def mastered_count() -> int:
    """复习队列总卡片数。"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM cards WHERE status = 'mastered'"
        ).fetchone()
    return row["n"]


def grade_card(card_id: int, grade: Grade) -> dict[str, Any] | None:
    """按自评结果更新 SM-2 三元组。返回更新后的状态；卡片不在复习队列返回 None。

    - remember：interval ×= ease，ease + 0.05（封顶 2.8）
    - fuzzy：interval ×= 1.2（至少 1 天），ease - 0.15
    - forgot：interval 重置为 1 天，ease - 0.2（下保底 1.3），明天重考
    """
    if grade not in GRADES:
        raise ValueError(f"非法复习评分: {grade}")

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, ease, interval FROM cards
            WHERE id = ? AND status = 'mastered' AND due_date IS NOT NULL
            """,
            (card_id,),
        ).fetchone()
        if not row:
            return None

        ease = row["ease"] or 2.5
        interval = row["interval"] or 1

        if grade == "remember":
            new_interval = max(1, round(interval * ease))
            new_ease = min(ease + 0.05, MAX_EASE)
        elif grade == "fuzzy":
            new_interval = max(1, round(interval * 1.2))
            new_ease = max(ease - 0.15, MIN_EASE)
        else:  # forgot
            new_interval = 1
            new_ease = max(ease - 0.2, MIN_EASE)

        new_due = (date.today() + timedelta(days=new_interval)).isoformat()
        conn.execute(
            "UPDATE cards SET ease = ?, interval = ?, due_date = ? WHERE id = ?",
            (round(new_ease, 2), new_interval, new_due, card_id),
        )
        # 记入 feedback 流水，供后续周报/统计使用
        conn.execute(
            "INSERT INTO feedback (card_id, action) VALUES (?, ?)",
            (card_id, f"review:{grade}"),
        )
        conn.commit()

    return {
        "card_id": card_id,
        "grade": grade,
        "ease": round(new_ease, 2),
        "interval": new_interval,
        "due_date": new_due,
    }
