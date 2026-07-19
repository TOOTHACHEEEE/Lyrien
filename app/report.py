"""学习周报（plan.md Phase 4）：每周日晚 LLM 汇总一周学习数据。

数据底座：cards / feedback（含 review:* 流水）/ notes / tags / tag_snapshots。
兴趣漂移靠 tag_snapshots 周快照积累 —— 历史从第一次生成周报那天开始攒。
"""

import json
from datetime import date, datetime, timedelta
from typing import Any

from app.db import get_db
from app.llm import chat_completion
from app.pipeline.summarize import load_prompt

# 手动生成任务状态（单进程内存态，与 feed.py 同一取舍）
GENERATE_STATUS: dict[str, Any] = {"state": "idle", "message": ""}


def week_bounds(ref: date | None = None) -> tuple[str, str]:
    """ref 所在周的周一 ~ 周日（ISO 日期）。"""
    ref = ref or date.today()
    monday = ref - timedelta(days=ref.weekday())
    sunday = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


def prev_week_start(week_start: str) -> str:
    monday = date.fromisoformat(week_start) - timedelta(days=7)
    return monday.isoformat()


def collect_week_stats(week_start: str, week_end: str) -> dict[str, Any]:
    """汇总一周的学习数据（纯查询，无 LLM，可直接单测）。"""
    with get_db() as conn:
        cards = conn.execute(
            """
            SELECT c.title, c.tags, c.is_explore, c.status, s.name AS source_name
            FROM cards c
            JOIN items i ON c.item_id = i.id
            JOIN sources s ON i.source_id = s.id
            WHERE c.date BETWEEN ? AND ?
            ORDER BY c.date, c.id
            """,
            (week_start, week_end),
        ).fetchall()

        feedback = conn.execute(
            """
            SELECT f.action, COUNT(*) AS n
            FROM feedback f
            JOIN cards c ON f.card_id = c.id
            WHERE f.created_at BETWEEN ? AND ?
            GROUP BY f.action
            """,
            (week_start, week_end + " 23:59:59"),
        ).fetchall()

        notes = conn.execute(
            """
            SELECT title FROM notes
            WHERE created_at BETWEEN ? AND ?
            ORDER BY id
            """,
            (week_start, week_end + " 23:59:59"),
        ).fetchall()

        top_tags = conn.execute(
            "SELECT name, weight FROM tags ORDER BY weight DESC, name LIMIT 10"
        ).fetchall()

        sources = conn.execute(
            "SELECT name, weight FROM sources WHERE type = 'rss' ORDER BY weight DESC"
        ).fetchall()

    fb_counts = {row["action"]: row["n"] for row in feedback}
    return {
        "cards": [dict(row) for row in cards],
        "feedback": fb_counts,
        "review_grades": {
            action.removeprefix("review:"): n
            for action, n in fb_counts.items()
            if action.startswith("review:")
        },
        "notes": [row["title"] for row in notes],
        "top_tags": [(row["name"], row["weight"]) for row in top_tags],
        "sources": [(row["name"], row["weight"]) for row in sources],
    }


def snapshot_tags(week_start: str) -> None:
    """把当前标签权重存为本周快照（upsert，可重复调用）。"""
    with get_db() as conn:
        rows = conn.execute("SELECT name, weight FROM tags").fetchall()
        for row in rows:
            conn.execute(
                """
                INSERT INTO tag_snapshots (tag, weight, week_start) VALUES (?, ?, ?)
                ON CONFLICT(tag, week_start) DO UPDATE SET weight = excluded.weight
                """,
                (row["name"], row["weight"], week_start),
            )
        conn.commit()


def get_snapshots(week_start: str) -> dict[str, float]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT tag, weight FROM tag_snapshots WHERE week_start = ?", (week_start,)
        ).fetchall()
    return {row["tag"]: row["weight"] for row in rows}


def interest_profile(limit: int = 8) -> list[dict[str, Any]]:
    """兴趣画像：当前 Top 标签权重 + 上周快照（算漂移，没有历史则为 None）。"""
    current_week, _ = week_bounds()
    prev = get_snapshots(prev_week_start(current_week))
    with get_db() as conn:
        rows = conn.execute(
            "SELECT name, weight FROM tags ORDER BY weight DESC, name LIMIT ?", (limit,)
        ).fetchall()
    return [
        {"name": row["name"], "weight": row["weight"], "prev": prev.get(row["name"])}
        for row in rows
    ]


def build_digest(stats: dict[str, Any], prev_snapshots: dict[str, float]) -> str:
    """把统计数据压成给 LLM 的文本摘要。"""
    lines: list[str] = []
    cards = stats["cards"]
    lines.append(f"本周卡片 {len(cards)} 张（含探索卡 {sum(1 for c in cards if c['is_explore'])} 张）：")
    for c in cards:
        tag_str = f" [标签: {c['tags']}]" if c["tags"] else ""
        explore = "（探索卡）" if c["is_explore"] else ""
        lines.append(f"- 《{c['title']}》 信源 {c['source_name']} 状态 {c['status']}{tag_str}{explore}")

    fb = stats["feedback"]
    if fb:
        readable = {k: v for k, v in fb.items() if not k.startswith("review:")}
        lines.append(f"\n反馈数据：{json.dumps(readable, ensure_ascii=False)}")
        if stats["review_grades"]:
            lines.append(f"复习自评：{json.dumps(stats['review_grades'], ensure_ascii=False)}")

    if stats["notes"]:
        lines.append(f"\n深挖笔记 {len(stats['notes'])} 篇：")
        for t in stats["notes"]:
            lines.append(f"- {t}")

    lines.append("\n当前兴趣标签权重 Top10：")
    for name, weight in stats["top_tags"]:
        prev_w = prev_snapshots.get(name)
        drift = f"（上周 {prev_w:.2f}）" if prev_w is not None else "（无上周快照）"
        lines.append(f"- {name}: {weight:.2f} {drift}")

    lines.append("\n信源信誉分：")
    for name, weight in stats["sources"]:
        lines.append(f"- {name}: {weight:.2f}")
    return "\n".join(lines)


def has_report(week_start: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM reports WHERE week_start = ?", (week_start,)
        ).fetchone()
    return row is not None


def report_due_now(now: datetime | None = None) -> bool:
    """本周日晚 21 点已过且本周报告缺失 → 启动时需要补跑。"""
    now = now or datetime.now()
    week_start, _ = week_bounds(now.date())
    due_moment = datetime.fromisoformat(week_start) + timedelta(days=6, hours=21)
    return now >= due_moment and not has_report(week_start)


async def generate_report(week_start: str | None = None) -> int:
    """生成（或覆盖重生成）一份周报，返回 report id。"""
    GENERATE_STATUS.update(state="pending", message="正在汇总一周数据…")
    try:
        ws, we = week_bounds() if week_start is None else (week_start, _week_end_for(week_start))
        stats = collect_week_stats(ws, we)
        prev_snapshots = get_snapshots(prev_week_start(ws))
        digest = build_digest(stats, prev_snapshots)

        GENERATE_STATUS.update(message="LLM 正在撰写周报…")
        prompt = load_prompt("report").format(
            week_start=ws, week_end=we, digest=digest
        )
        content = await chat_completion(
            [
                {"role": "system", "content": "你是一位善于观察的学习教练。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.6,
            max_retries=2,
        )
        if not content.strip():
            raise RuntimeError("LLM 返回了空内容")

        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO reports (week_start, week_end, content) VALUES (?, ?, ?)
                ON CONFLICT(week_start) DO UPDATE SET content = excluded.content,
                    week_end = excluded.week_end, created_at = CURRENT_TIMESTAMP
                """,
                (ws, we, content),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM reports WHERE week_start = ?", (ws,)
            ).fetchone()
        snapshot_tags(ws)
        GENERATE_STATUS.update(state="done", message="周报已生成")
        return row["id"]
    except Exception as exc:
        GENERATE_STATUS.update(state="error", message=f"生成失败: {exc}")
        raise


def _week_end_for(week_start: str) -> str:
    return (date.fromisoformat(week_start) + timedelta(days=6)).isoformat()


def latest_report() -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, week_start, week_end, content, created_at FROM reports ORDER BY week_start DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def get_report(report_id: int) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, week_start, week_end, content, created_at FROM reports WHERE id = ?",
            (report_id,),
        ).fetchone()
    return dict(row) if row else None


def list_reports(limit: int = 20) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, week_start, week_end, created_at FROM reports ORDER BY week_start DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
