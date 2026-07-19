"""管线编排与定时任务：抓取 → 初筛 → 精选 → 抓全文 → 生成卡片。"""

import asyncio
from datetime import date
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import get_db, init_db
from app.feedback import weekly_tag_decay
from app.pipeline.fetch import fetch_all
from app.pipeline.summarize import generate_cards

scheduler = AsyncIOScheduler()


def has_today_cards() -> bool:
    """检查今天是否已有卡片。"""
    return today_card_count() > 0


def today_card_count() -> int:
    """今日卡片数量。"""
    today = date.today().isoformat()
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM cards WHERE date = ?", (today,)
        ).fetchone()
    return row["n"]


async def run_pipeline(force: bool = False) -> dict[str, Any]:
    """异步运行完整管线。

    - 默认幂等：今日已有卡片时只抓新条目，不重复生成（定时任务/启动补跑安全）。
    - force=True（手动刷新）：若今日不足 cards_per_day 张则补差额，已满则不动，
      避免重复点击无限追加当日卡片。
    """
    print("[jobs] 管线启动")
    init_db()

    new_item_ids = fetch_all()

    card_ids: list[int] = []
    existing = today_card_count()
    if existing and not force:
        print(f"[jobs] 今日已有 {existing} 张卡片，跳过生成")
    elif existing >= settings.cards_per_day:
        print(f"[jobs] 今日卡片已达 {existing}/{settings.cards_per_day}，不再追加")
    else:
        remaining = settings.cards_per_day - existing
        card_ids = await generate_cards(k=remaining)

    result = {
        "date": date.today().isoformat(),
        "new_items": len(new_item_ids),
        "new_cards": len(card_ids),
        "card_ids": card_ids,
    }
    print(f"[jobs] 管线完成: {result}")
    return result


def run_pipeline_sync() -> dict[str, Any]:
    """同步入口（脚本/启动补跑用）。"""
    return asyncio.run(run_pipeline())


def setup_scheduler() -> AsyncIOScheduler:
    """注册定时任务：每日管线 + 每周兴趣标签衰减 + 每周学习周报。"""
    from app.report import generate_report  # 避免循环导入，用时再引

    scheduler.add_job(
        run_pipeline,
        CronTrigger(hour=settings.daily_run_hour, minute=0),
        id="daily_pipeline",
        name="每日卡片管线",
        replace_existing=True,
    )
    scheduler.add_job(
        weekly_tag_decay,
        CronTrigger(day_of_week="mon", hour=3, minute=0),
        id="weekly_tag_decay",
        name="兴趣标签周衰减",
        replace_existing=True,
    )
    scheduler.add_job(
        generate_report,
        CronTrigger(day_of_week="sun", hour=21, minute=0),
        id="weekly_report",
        name="每周学习周报",
        replace_existing=True,
    )
    return scheduler


if __name__ == "__main__":
    result = run_pipeline_sync()
    print(result)
