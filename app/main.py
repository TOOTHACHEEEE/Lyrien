"""FastAPI 入口 + 前端路由。"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.db import get_db, init_db
from app.feed import feed_status, start_feed
from app.feedback import ACTIONS, apply_feedback
from app.llm import chat_completion_stream
from app.notes import (
    get_note,
    list_notes,
    note_count,
    note_for_card,
    random_old_note,
    related_notes,
    save_note,
    search_notes,
    update_note,
)
from app.pipeline.jobs import run_pipeline, scheduler, setup_scheduler, today_card_count
from app.pipeline.summarize import load_prompt
from app.qa import build_context, retrieve_notes
from app.report import (
    GENERATE_STATUS,
    generate_report,
    get_report,
    interest_profile,
    latest_report,
    list_reports,
    report_due_now,
)
from app.review import GRADES, due_count, get_due_cards, grade_card, mastered_count

templates = Jinja2Templates(directory="app/templates")

# 深挖流式输出结束时的哨兵行：前端据此渲染"已存入知识库"链接
NOTE_SAVED_PREFIX = "__NOTE_SAVED__:"
# 问答流式输出结束时的哨兵行：带回引用笔记清单
SOURCES_PREFIX = "__SOURCES__:"


def get_today_cards() -> list[dict]:
    """查询今日卡片。"""
    today = date.today().isoformat()
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.title, c.summary_json, c.tags, c.is_explore,
                   c.status, i.url, i.fulltext, s.name AS source_name
            FROM cards c
            JOIN items i ON c.item_id = i.id
            JOIN sources s ON i.source_id = s.id
            WHERE c.date = ?
            ORDER BY c.is_explore, c.id
            """,
            (today,),
        ).fetchall()
    cards = []
    for row in rows:
        card = dict(row)
        card["summary"] = parse_summary(card["summary_json"])
        cards.append(card)
    return cards


def parse_summary(summary_json: str) -> dict:
    """解析卡片 summary_json，兜底为纯文本核心概念。"""
    try:
        summary = json.loads(summary_json)
    except json.JSONDecodeError:
        return {"core_concept": summary_json, "why_learn": "", "guide_question": ""}

    related = summary.get("related_knowledge", [])
    if isinstance(related, list):
        summary["related_knowledge"] = ", ".join(str(x) for x in related)
    elif related is None:
        summary["related_knowledge"] = ""
    return summary


def page_context(**extra):
    """全页渲染的公共上下文（导航角标等）。"""
    return {"review_due": due_count(), **extra}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时初始化数据库、启动定时任务并检查今日卡片。"""
    init_db()
    setup_scheduler()
    scheduler.start()
    for job in scheduler.get_jobs():
        print(f"[scheduler] 已注册 {job.name} (id={job.id})，下次运行: {job.next_run_time}")
    if today_card_count() < settings.cards_per_day:
        print("[main] 今日卡片不足配额，后台补跑管线...")
        asyncio.create_task(run_pipeline())
    else:
        print("[main] 今日卡片已达配额")
    if report_due_now():
        print("[main] 本周周报缺失，后台补跑生成...")
        asyncio.create_task(_safe_generate_report())
    yield
    scheduler.shutdown(wait=False)


async def _safe_generate_report() -> None:
    """后台生成周报，异常只打日志不影响应用。"""
    try:
        await generate_report()
    except Exception as exc:
        print(f"[main] 周报生成失败: {exc}")


app = FastAPI(title="Lyrien", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    cards = get_today_cards()
    return templates.TemplateResponse(
        request,
        "index.html",
        page_context(
            cards=cards,
            date=date.today().isoformat(),
            quota=settings.cards_per_day,
        ),
    )


@app.get("/cards-partial", response_class=HTMLResponse)
async def cards_partial(request: Request):
    """HTMX 轮询用：只返回卡片列表片段。"""
    cards = get_today_cards()
    return templates.TemplateResponse(
        request,
        "partials/card_list.html",
        {
            "request": request,
            "cards": cards,
            "date": date.today().isoformat(),
            "quota": settings.cards_per_day,
        },
    )


@app.get("/pet-status")
async def pet_status():
    """桌宠状态源：今日卡片进度 + 待复习数，供 pet.js 决定播报内容。"""
    cards = today_card_count()
    return {
        "cards": cards,
        "quota": settings.cards_per_day,
        "cooking": cards == 0,
        "review_due": due_count(),
    }


@app.post("/refresh")
async def refresh():
    """手动触发后台管线（补差额模式，不会重复生成已满的当日卡片）。"""
    asyncio.create_task(run_pipeline(force=True))
    return {"status": "started"}


@app.post("/feedback/{card_id}/{action}", response_class=HTMLResponse)
async def submit_feedback(card_id: int, action: str):
    """接收 👍/👎/💡 反馈，更新兴趣画像，返回替换按钮组的 HTML 片段。"""
    if action not in ACTIONS:
        return HTMLResponse(
            '<span class="feedback-note">未知的反馈动作</span>', status_code=400
        )
    if not apply_feedback(card_id, action):
        return HTMLResponse(
            '<span class="feedback-note">卡片不存在</span>', status_code=404
        )
    if action == "mastered":
        note = "已加入复习队列 · 明天再考考你"
    elif action == "up":
        note = "已记录 · 明天的推荐会更懂你"
    else:
        note = "已记录 · 这类内容会减少出现"
    return HTMLResponse(f'<span class="feedback-note">{note}</span>')


# ============================================================
# Phase 3 · 深挖：卡片 → 流式教程 → 存入知识库
# ============================================================


@app.get("/dig/{card_id}")
async def dig_card(card_id: int):
    """深挖：基于卡片全文流式生成教程，完成后自动存入 notes 并回传 note_id。"""
    existing = note_for_card(card_id)
    if existing:
        async def already():
            yield "这张卡片已经深挖过了，教程在知识库里。"
            yield f"\n{NOTE_SAVED_PREFIX}{existing['id']}"

        return StreamingResponse(already(), media_type="text/plain; charset=utf-8")

    with get_db() as conn:
        row = conn.execute(
            """
            SELECT i.fulltext, i.summary, i.title, i.url
            FROM cards c JOIN items i ON c.item_id = i.id
            WHERE c.id = ?
            """,
            (card_id,),
        ).fetchone()
    if not row:
        return StreamingResponse(iter(["卡片不存在"]), media_type="text/plain; charset=utf-8")

    body = (row["fulltext"] or row["summary"] or "")[:8000]
    prompt = load_prompt("dig").format(title=row["title"], url=row["url"], fulltext=body)
    messages = [
        {"role": "system", "content": "你是一位严谨的技术导师。"},
        {"role": "user", "content": prompt},
    ]
    card_title = row["title"]

    async def event_generator():
        chunks: list[str] = []
        # 只有流正常跑完才落库。客户端中途断开（页面刷新/导航/轮询替换 DOM）
        # 时生成器在 yield 处被关闭，不会执行到落库 —— 否则半截教程会被当成
        # "已深挖过"（note_for_card 命中），永远无法重新生成。
        async for chunk in chat_completion_stream(messages, temperature=0.6):
            chunks.append(chunk)
            yield chunk
        content = "".join(chunks).strip()
        if content:
            note_id = save_note(card_title, content, card_id=card_id)
            yield f"\n{NOTE_SAVED_PREFIX}{note_id}"

    return StreamingResponse(event_generator(), media_type="text/plain; charset=utf-8")


# ============================================================
# Phase 3 · 知识库：列表 + FTS5 搜索 + 笔记详情/关联
# ============================================================


@app.get("/kb", response_class=HTMLResponse)
async def kb_index(request: Request):
    return templates.TemplateResponse(
        request,
        "kb.html",
        page_context(notes=list_notes(), total=note_count(), query=""),
    )


@app.get("/kb/search", response_class=HTMLResponse)
async def kb_search(request: Request, q: str = ""):
    """知识库搜索（HTMX 片段）。空查询返回最近笔记。"""
    q = q.strip()
    results = search_notes(q) if q else list_notes()
    return templates.TemplateResponse(
        request,
        "partials/note_list.html",
        {"request": request, "notes": results, "query": q},
    )


# 注意：/notes/new 必须注册在 /notes/{note_id} 之前，否则被路径参数吞掉
@app.get("/notes/new", response_class=HTMLResponse)
async def note_new(request: Request):
    """写笔记：空白编辑器。"""
    return templates.TemplateResponse(
        request,
        "note_edit.html",
        page_context(note=None),
    )


@app.post("/notes/new")
async def note_create(request: Request):
    form = await request.form()
    title = str(form.get("title", "")).strip()
    content = str(form.get("content", "")).strip()
    tags = str(form.get("tags", "")).strip()
    if not title or not content:
        return HTMLResponse("标题和正文不能为空", status_code=400)
    note_id = save_note(title, content, tags=tags)
    return RedirectResponse(f"/notes/{note_id}", status_code=303)


@app.get("/notes/{note_id}/edit", response_class=HTMLResponse)
async def note_edit(request: Request, note_id: int):
    note = get_note(note_id)
    if not note:
        return HTMLResponse("笔记不存在", status_code=404)
    return templates.TemplateResponse(
        request,
        "note_edit.html",
        page_context(note=note),
    )


@app.post("/notes/{note_id}/edit")
async def note_update(request: Request, note_id: int):
    form = await request.form()
    title = str(form.get("title", "")).strip()
    content = str(form.get("content", "")).strip()
    tags = str(form.get("tags", "")).strip()
    if not title or not content:
        return HTMLResponse("标题和正文不能为空", status_code=400)
    if not update_note(note_id, title, content, tags=tags):
        return HTMLResponse("笔记不存在", status_code=404)
    return RedirectResponse(f"/notes/{note_id}", status_code=303)


@app.get("/notes/{note_id}", response_class=HTMLResponse)
async def note_detail(request: Request, note_id: int):
    note = get_note(note_id)
    if not note:
        return HTMLResponse("笔记不存在", status_code=404)
    return templates.TemplateResponse(
        request,
        "note_detail.html",
        page_context(note=note, related=related_notes(note_id)),
    )


# ============================================================
# Phase 3 · 复习：每日复习页 + 自评（SM-2）+ AI 出题
# ============================================================


@app.get("/review", response_class=HTMLResponse)
async def review_page(request: Request):
    due = []
    for card in get_due_cards():
        card["summary"] = parse_summary(card["summary_json"])
        due.append(card)
    return templates.TemplateResponse(
        request,
        "review.html",
        page_context(
            cards=due,
            mastered_total=mastered_count(),
            archaeology=random_old_note(),
        ),
    )


@app.post("/review/{card_id}/grade/{grade}", response_class=HTMLResponse)
async def review_grade(request: Request, card_id: int, grade: str):
    """自评评分：更新 SM-2 三元组，返回替换该卡片的片段（含导航角标 OOB 更新）。"""
    if grade not in GRADES:
        return HTMLResponse("非法评分", status_code=400)
    result = grade_card(card_id, grade)
    if result is None:
        return HTMLResponse("卡片不在复习队列", status_code=404)
    return templates.TemplateResponse(
        request,
        "partials/grade_result.html",
        {"request": request, "result": result, "review_due": due_count()},
    )


@app.get("/review/{card_id}/quiz")
async def review_quiz(card_id: int):
    """AI 出题：基于卡片全文流式生成 3 道复习自测题。"""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT i.fulltext, i.summary, i.title
            FROM cards c JOIN items i ON c.item_id = i.id
            WHERE c.id = ?
            """,
            (card_id,),
        ).fetchone()
    if not row:
        return StreamingResponse(iter(["卡片不存在"]), media_type="text/plain; charset=utf-8")

    body = (row["fulltext"] or row["summary"] or "")[:6000]
    prompt = load_prompt("quiz").format(title=row["title"], fulltext=body)
    messages = [
        {"role": "system", "content": "你是一位严格但友善的导师。"},
        {"role": "user", "content": prompt},
    ]

    async def event_generator():
        async for chunk in chat_completion_stream(messages, temperature=0.5):
            yield chunk

    return StreamingResponse(event_generator(), media_type="text/plain; charset=utf-8")


@app.get("/review/{card_id}/explain")
async def review_explain(card_id: int):
    """AI 讲解：复习时想不起来，基于卡片全文流式重建理解（只读辅助，不入库）。"""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT i.fulltext, i.summary, i.title
            FROM cards c JOIN items i ON c.item_id = i.id
            WHERE c.id = ?
            """,
            (card_id,),
        ).fetchone()
    if not row:
        return StreamingResponse(iter(["卡片不存在"]), media_type="text/plain; charset=utf-8")

    body = (row["fulltext"] or row["summary"] or "")[:6000]
    prompt = load_prompt("explain").format(title=row["title"], fulltext=body)
    messages = [
        {"role": "system", "content": "你是一位善于帮人回忆的导师。"},
        {"role": "user", "content": prompt},
    ]

    async def event_generator():
        async for chunk in chat_completion_stream(messages, temperature=0.5):
            yield chunk

    return StreamingResponse(event_generator(), media_type="text/plain; charset=utf-8")


# ============================================================
# Phase 4 · 手动投喂：粘贴 URL → 同一管线生成卡片
# ============================================================


@app.post("/feed")
async def feed(request: Request):
    """接收投喂的 URL，后台异步生成卡片；返回 url_hash 供前端轮询。"""
    form = await request.form()
    url = str(form.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        return {"error": "请输入 http(s) 开头的链接"}
    return {"h": start_feed(url)}


@app.get("/feed/status")
async def feed_task_status(h: str):
    return feed_status(h)


# ============================================================
# Phase 4 · 知识库问答：FTS5 召回 + LLM 带引用作答
# ============================================================


@app.get("/ask", response_class=HTMLResponse)
async def ask_page(request: Request):
    return templates.TemplateResponse(request, "ask.html", page_context())


@app.get("/ask/stream")
async def ask_stream(q: str = ""):
    """流式回答：先 FTS5 召回笔记，再让 LLM 基于笔记作答，末尾回传引用清单。"""
    q = q.strip()
    if not q:
        return StreamingResponse(iter(["先提个问题吧。"]), media_type="text/plain; charset=utf-8")

    notes = retrieve_notes(q)
    if not notes:
        async def no_notes():
            yield "知识库里还没有相关的内容 —— 先去投喂几篇文章，或对卡片做几次深挖吧。"

        return StreamingResponse(no_notes(), media_type="text/plain; charset=utf-8")

    context = build_context(notes)
    prompt = load_prompt("ask").format(context=context)
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": q},
    ]
    sources = [{"id": n["id"], "title": n["title"]} for n in notes]

    async def event_generator():
        async for chunk in chat_completion_stream(messages, temperature=0.4):
            yield chunk
        yield f"\n{SOURCES_PREFIX}{json.dumps(sources, ensure_ascii=False)}"

    return StreamingResponse(event_generator(), media_type="text/plain; charset=utf-8")


# ============================================================
# Phase 4 · 学习周报：周日晚定时 + 启动补跑 + 手动生成
# ============================================================


@app.get("/report", response_class=HTMLResponse)
async def report_page(request: Request):
    return templates.TemplateResponse(
        request,
        "report.html",
        page_context(
            report=latest_report(),
            history=list_reports(),
            profile=interest_profile(),
        ),
    )


@app.post("/report/generate")
async def report_generate():
    """手动生成/重新生成本周周报（后台异步，前端轮询状态）。"""
    if GENERATE_STATUS.get("state") != "pending":
        asyncio.get_running_loop().create_task(_safe_generate_report())
    return {"status": "started"}


# 注意：/report/status 必须注册在 /report/{report_id} 之前，否则被路径参数吞掉
@app.get("/report/status")
async def report_status():
    return GENERATE_STATUS


@app.get("/report/{report_id}", response_class=HTMLResponse)
async def report_detail(request: Request, report_id: int):
    report = get_report(report_id)
    if not report:
        return HTMLResponse("周报不存在", status_code=404)
    return templates.TemplateResponse(
        request,
        "report.html",
        page_context(
            report=report,
            history=list_reports(),
            profile=interest_profile(),
        ),
    )
