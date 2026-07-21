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

# 管线互斥锁：启动补跑、手动刷新、定时任务可能叠加触发，防止并发重入
_pipeline_lock = asyncio.Lock()


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

    无论定时任务、启动补跑还是手动刷新（force），都以 cards_per_day 为上限补差额：
    今日不足配额则补足，已满则不动。force 参数保留仅为兼容旧调用方，行为已无差别。
    同一时刻只允许一个管线运行，重复触发直接跳过。
    """
    if _pipeline_lock.locked():
        print("[jobs] 管线已在运行，跳过本次触发")
        return {"date": date.today().isoformat(), "new_items": 0, "new_cards": 0,
                "card_ids": [], "skipped": "already_running"}
    async with _pipeline_lock:
        try:
            return await _run_pipeline_locked()
        except Exception as exc:
            # create_task 触发的异常无人接收，这里兜底记录，避免静默失败
            print(f"[jobs] 管线异常: {type(exc).__name__}: {exc}")
            return {"date": date.today().isoformat(), "new_items": 0, "new_cards": 0,
                    "card_ids": [], "error": f"{type(exc).__name__}: {exc}"}


async def _run_pipeline_locked() -> dict[str, Any]:
    print("[jobs] 管线启动")
    init_db()

    new_item_ids = fetch_all()

    card_ids: list[int] = []
    existing = today_card_count()
    if existing >= settings.cards_per_day:
        print(f"[jobs] 今日卡片已达 {existing}/{settings.cards_per_day}，不再追加")
    else:
        remaining = settings.cards_per_day - existing
        if existing:
            print(f"[jobs] 今日已有 {existing} 张卡片，补足差额 {remaining} 张")
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
