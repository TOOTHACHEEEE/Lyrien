# Lyrien

> 个人 AI 学习引擎 —— 每天早上打开它，3~5 张为你量身定制的高质量学习卡片已经躺在那里。
> 你只管学，抓取、筛选、总结、复习提醒，全部由后台默默完成。

单人自用的本地 Web 应用。FastAPI + SQLite 单文件，零构建前端（Jinja2 + HTMX + Tailwind CDN），
UI 为「芙宁娜歌剧院」主题：每日卡片是一出剧目的若干幕。

---

## 快速开始

```bash
cp .env.example .env      # 填入 LLM_API_KEY（默认走 DeepSeek，兼容 OpenAI 接口）
./start.sh                # 自动建 venv、装依赖、起服务（带 --reload）
# 打开 http://127.0.0.1:8000
```

常用命令：

```bash
.venv/bin/python -m pytest tests/        # 跑测试
.venv/bin/python -m ruff check app tests # 静态检查
.venv/bin/python -m app.pipeline.jobs    # 手动跑一次每日管线
```

## 功能全景

### 每日学习卡片（Phase 1 · 已完成）
- RSS 抓取（feedparser）→ URL 哈希去重 → 入库
- LLM 精选 + 结构化卡片生成：核心概念 / 为何值得学 / 关联旧知 / 引导思考
- 卡片必附原始链接，Prompt 明令禁止编造正文中没有的事实
- 启动补跑：凌晨关机错过定时任务？打开应用自动后台补跑，页面轮询自动刷新

### 质量与反馈闭环（Phase 2 · 已完成）
- trafilatura 全文提取：只对精选出的条目抓全文，失败降级用摘要
- 两阶段过滤：规则+标签关键词零成本初筛（100→15），LLM 只做精选不当生成器
- 探索卡机制：每天最多 1 张圈外高质量内容，防信息茧房
- 👍/👎/💡 反馈实时更新兴趣标签权重与信源信誉分；兴趣权重每周衰减归一化
- APScheduler 每日定时管线

### 知识库与复习（Phase 3 · 已完成）
- **深挖成教程**：卡片 → 流式生成 Markdown 教程（真·打字机效果）→ 自动存入知识库。
  幂等设计：同一张卡片不会重复深挖，再次点击直接指向已有笔记
- **知识库 `/kb`**：笔记列表 + FTS5 全文搜索（自研 CJK 字级分词，中文可搜，零依赖）
  + 搜索结果关键词高亮 + 笔记详情页「关联旧知」（按共享标签回溯新旧知识）
- **每日复习 `/review`**：简化 SM-2 间隔重复。💡已掌握的卡片按 1→3→7 天…节奏到期；
  自评 忘记/模糊/记得 三档调整间隔与难度因子；内置引导思考题自测，也可一键 **AI 出题**
  （流式生成 3 道递进问题）；评分后导航角标实时更新
- 主导航：今日卡片 / 复习（到期数角标）/ 知识库

### 打磨与扩展（Phase 4 · 已完成第一批）
- **📥 手动投喂**：首页粘贴任意 URL → 走同一管线（全文提取 → LLM 卡片）生成当日卡片，
  不受每日配额限制；URL 哈希去重，已投喂过的直接指向旧卡；后台异步 + 前端轮询状态
- **💬 知识库问答 `/ask`**：RAG-lite —— FTS5 召回 Top5 笔记 → LLM 基于笔记作答（流式），
  引用《笔记标题》注明出处，回答末尾回传可点击的引用清单。**刻意没上 sqlite-vec**：
  默认 LLM（DeepSeek）没有 embeddings 接口，几百篇量级下关键词召回足够
- **📊 学习周报 `/report`**：周日晚 21 点自动生成（启动补跑兜底，笔记本关机也不怕），
  也可手动随时生成；LLM 基于一周数据摘要（卡片/反馈/复习自评/笔记/信源分）撰写复盘；
  **兴趣画像**：标签权重条形图 + 每周快照（tag_snapshots）对比漂移 ▲▼
- **🕰️ 知识考古**：复习页随机翻出一篇 7 天前的旧笔记问"还记得吗"
- **✍️ 内置 Markdown 编辑器**：`/kb` 点「写笔记」随时记录。左右分栏实时预览、
  Ctrl/Cmd+S 保存、标签参与「关联旧知」与全文搜索；**localStorage 草稿兜底** ——
  击键即存本机浏览器，后端重启/合盖/断网都丢不了未保存的内容，重开页面可一键恢复

### 下一步（候选，按需挑选）
- 📱 PWA 化（桌面图标）或 Tauri 套壳
- 🧪 周测验（LLM 根据本周卡片出 5 道题）
- ✂️ 知识库自动修剪：合并相似旧笔记
- 🎙️ 播客/视频信源（YouTube 字幕走同一管线）
- 💬 向量版问答（若哪天接入带 embeddings 的模型，再上 sqlite-vec）

## 技术架构

```
浏览器（Jinja2 + HTMX + Tailwind CDN + marked/DOMPurify）
  │  零构建，改完即刷
FastAPI 单进程
  ├── pipeline/    抓取 fetch → 初筛 filter → 精选+卡片 summarize → 编排 jobs
  ├── feed.py      手动投喂：URL → 全文 → 卡片（后台任务 + 状态轮询）
  ├── notes.py     知识库：CJK 分词 FTS5 检索、关联查询、知识考古
  ├── qa.py        问答 RAG-lite：FTS5 召回 → 上下文拼装
  ├── review.py    简化 SM-2 复习队列
  ├── report.py    学习周报：数据汇总 → LLM 撰写 → 标签快照
  ├── feedback.py  反馈闭环：标签权重 + 信源信誉
  └── llm.py       OpenAI 兼容客户端（JSON 重试修复 + 流式）
SQLite 单文件（data/lyrien.db）
  └── sources / items / cards / feedback / tags / notes / notes_fts(FTS5)
      / reports / tag_snapshots
LLM：OpenAI 兼容接口，默认 DeepSeek；改 .env 一行即可换 Claude/Ollama
```

成本设计：初筛不用大模型；每日 LLM 调用 = 精选 1 次 + 卡片 ~5 次，DeepSeek 量级约每天几分钱。

## 目录结构

```
lyrien/
├── plan.md                  # 项目计划书（愿景/机制/路线图）
├── README.md                # 本文件：功能与进度总览
├── Advices.md               # 创意建议书：评估方法论 + 分梯队的下一步提案
├── pyproject.toml
├── .env / .env.example      # LLM_API_KEY 等（.env 不提交）
├── start.sh                 # 一键启动
├── app/
│   ├── main.py              # FastAPI 入口 + 全部路由
│   ├── config.py            # .env 配置（pydantic-settings）
│   ├── db.py                # SQLite 连接 + 建表（含 notes_fts FTS5）
│   ├── llm.py               # OpenAI 兼容客户端（流式/JSON 重试）
│   ├── feedback.py          # 👍/👎/💡 权重闭环 + 周衰减
│   ├── feed.py              # 手动投喂管线（URL → 卡片）
│   ├── notes.py             # 知识库笔记：保存/搜索/关联/考古（CJK 分词）
│   ├── qa.py                # 问答召回：FTS5 检索 + 上下文拼装
│   ├── report.py            # 学习周报：数据汇总 + 生成 + 标签快照
│   ├── review.py            # 简化 SM-2：到期查询 + 三档自评
│   ├── pipeline/
│   │   ├── fetch.py         # RSS 抓取 + trafilatura 全文/标题提取
│   │   ├── filter.py        # 零成本初筛（兴趣池/探索池）
│   │   ├── summarize.py     # LLM 精选 + 卡片生成
│   │   └── jobs.py          # APScheduler：每日管线 + 周报 + 启动补跑
│   ├── prompts/             # 所有 Prompt 独立成文件
│   │   ├── select.md        # 精选（兴趣池+探索池）
│   │   ├── card.md          # 卡片结构化生成
│   │   ├── dig.md           # 深挖教程
│   │   ├── quiz.md          # 复习出题
│   │   ├── ask.md           # 知识库问答
│   │   └── report.md        # 学习周报
│   ├── templates/           # Jinja2（index/kb/note_detail/review/ask/report + partials）
│   └── static/              # style.css（芙宁娜主题）+ script.js（流式交互）
├── data/lyrien.db           # 单文件数据库（gitignore）
└── tests/                   # pytest 离线单测（不触网、不触 LLM）
```

## 主要路由

| 路由 | 说明 |
|---|---|
| `GET /` | 今日卡片页（无卡自动进入"烹饪中"轮询态） |
| `POST /refresh` | 手动补跑管线（补差额，不重复生成） |
| `POST /feedback/{card_id}/{action}` | 👍/👎/💡 反馈 |
| `GET /dig/{card_id}` | 流式深挖教程，末尾回传 `__NOTE_SAVED__:<id>` 哨兵 |
| `GET /kb` · `GET /kb/search?q=` | 知识库列表 / FTS5 搜索片段 |
| `GET /notes/{note_id}` | 笔记详情（Markdown 渲染 + 关联旧知） |
| `GET/POST /notes/new` | 写笔记（编辑器 + localStorage 草稿） |
| `GET/POST /notes/{note_id}/edit` | 编辑笔记（FTS 实时重同步） |
| `GET /review` | 每日复习页（含知识考古） |
| `POST /review/{card_id}/grade/{grade}` | 三档自评（remember/fuzzy/forgot），OOB 更新角标 |
| `GET /review/{card_id}/quiz` | AI 出题（流式 3 道递进自测题） |
| `POST /feed` · `GET /feed/status` | 手动投喂：提交 URL / 轮询生成状态 |
| `GET /ask` · `GET /ask/stream?q=` | 问答页 / 流式回答（哨兵 `__SOURCES__:` 回传引用） |
| `GET /report` · `GET /report/{id}` | 最新周报 / 历史周报 |
| `POST /report/generate` · `GET /report/status` | 手动生成周报 / 轮询状态 |

## 关键设计决策（Phase 3 对 plan 的细化）

- **深挖幂等**：`notes.card_id` 唯一定位，已深挖的卡片直接返回已有笔记，不重复消耗 LLM。
- **流式存库协议**：服务端边生成边转发，流结束后入库并追加一行哨兵
  `__NOTE_SAVED__:<note_id>`；前端据此渲染「已存入知识库」链接。纯文本流与元数据同通道，无需二次请求。
- **中文 FTS5**：unicode61 分词器不会切中文，采用「CJK 逐字插空格」的字级索引
  （`notes.py: segment_cjk`），入库与查询同一变换，搜索摘要展示前还原空格并只放行 `<mark>` 高亮。
- **SM-2 三档简化**：记得 interval×ease / 模糊 interval×1.2 / 忘记重置 1 天；
  ease 钳制在 [1.3, 2.8]；每次评分写 `feedback` 流水（`review:*`），为周报攒数据。
- **前端流式**：深挖/出题用 fetch + ReadableStream 实现真打字机（HTMX 不支持流式），
  完成后用 marked + DOMPurify 渲染 Markdown。

## 关键设计决策（Phase 4 对 plan 的细化）

- **RAG-lite 取代 sqlite-vec**：默认 LLM（DeepSeek）没有 embeddings 接口，上向量库就要
  再配一家 embedding 服务。几百篇笔记量级下，FTS5 关键词召回 + LLM 阅读全文已够用；
  引用清单走 `__SOURCES__:` 哨兵（与深挖的 `__NOTE_SAVED__:` 同一协议）。
- **后台任务状态放内存**：投喂/周报都是 30 秒级的按需任务，单进程字典 + 前端轮询即可，
  不引入任务队列；重启只丢"进行中"提示，不丢数据。
- **兴趣漂移用周快照**：`tag_snapshots` 在每次生成周报时 upsert 一份权重快照，
  历史从第一份周报开始攒 —— 不伪造没有的数据。
- **周报也守补跑哲学**：周日晚 21 点定时 + 启动时检查"本周日晚已过且无报告"自动补跑，
  与每日卡片的启动补跑同一逻辑（笔记本党友好）。
- **投喂走 manual 信源**：内置"手动投喂"信源（type=manual），RSS 抓取只扫 type=rss，
  互不影响；投喂不占每日 cards_per_day 配额。
- **编辑器草稿只进 localStorage，不做服务端自动保存**：已提交的数据 SQLite 落盘零风险，
  会丢的只有输入框内容 —— 草稿击键写本机浏览器即可全覆盖，服务端自动保存反而会把
  半成品刷进知识库列表。标签让手动笔记与深挖笔记同权参与关联和搜索。

## 进度追踪

| 阶段 | 状态 | 验收 |
|---|---|---|
| Phase 1 最小可用闭环 | ✅ 完成 | 跑一次管线，页面看到 ≥3 张带链接卡片 |
| Phase 2 质量与反馈 | ✅ 完成 | 👍/👎/💡 生效；关机错过定时次日自动补跑 |
| Phase 3 知识库与复习 | ✅ 完成 | 深挖可搜到教程；到期卡片准时出现在复习页 |
| Phase 4 第一批（投喂/问答/周报/考古） | ✅ 完成 | 投喂 URL 出卡片；问答带引用；周报可手动生成并看到画像漂移 |
| Phase 4 剩余候选（PWA/周测验/修剪/视频源） | ⬜ 按需 | 见上方候选清单 |

测试：`tests/` 40 项离线单测全绿（反馈闭环 / 初筛 / 笔记检索 / SM-2 / 投喂 / 问答 / 周报）。
