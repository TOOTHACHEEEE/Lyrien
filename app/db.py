"""SQLite 连接与表结构初始化。"""

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import settings

# FTS5 的 unicode61 分词器不会切中文：入库/查询前对 CJK 逐字插空格（字级索引）。
# 放在 db.py 是因为 FTS 表结构与迁移都由本文件负责。
_CJK_RE = re.compile(r"([㐀-䶿一-鿿豈-﫿])")


def segment_cjk(text: str) -> str:
    """在每个 CJK 字符后插入空格，让 unicode61 能按字检索中文。"""
    return _CJK_RE.sub(r"\1 ", text)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT 'rss',
    url TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    summary TEXT,
    fulltext TEXT,
    fetched_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    url_hash TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    item_id INTEGER NOT NULL UNIQUE REFERENCES items(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    tags TEXT,
    is_explore INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',
    ease REAL,
    interval INTEGER,
    due_date TEXT
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    weight REAL NOT NULL DEFAULT 0.0,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    card_id INTEGER REFERENCES cards(id) ON DELETE SET NULL,
    tags TEXT,                              -- 手动笔记的标签（逗号分隔），深挖笔记为 NULL
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP                    -- 最后编辑时间，NULL 表示从未编辑
);

-- 笔记全文检索（FTS5）。unicode61 分词器不会切中文，
-- 因此本表存的是 segment_cjk() 逐字分词后的文本，由代码手动同步（不用触发器）。
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(title, content, tags);

-- 学习周报（plan.md Phase 4）：每周一份，week_start 为周一 ISO 日期
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL UNIQUE,
    week_end TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 兴趣标签周快照：每次生成周报时记录，用于观察兴趣漂移（兴趣星图的数据底座）
CREATE TABLE IF NOT EXISTS tag_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL,
    weight REAL NOT NULL,
    week_start TEXT NOT NULL,
    UNIQUE(tag, week_start)
);

-- 初始化默认信源
INSERT OR IGNORE INTO sources (name, type, url, weight) VALUES
    ('Hacker News', 'rss', 'https://news.ycombinator.com/rss', 1.0),
    ('LWN.net', 'rss', 'https://lwn.net/headlines/rss', 1.0),
    ('arXiv cs.CR', 'rss', 'http://export.arxiv.org/rss/cs.CR', 1.0),
    ('手动投喂', 'manual', 'manual://feed', 1.0);

-- 初始化默认兴趣标签
INSERT OR IGNORE INTO tags (name, weight) VALUES
    ('网络安全', 1.0),
    ('C++', 1.0),
    ('统计学', 1.0),
    ('Linux 内核', 0.8),
    ('机器学习', 0.5);
"""


def ensure_db_dir() -> None:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)


def init_db() -> None:
    """创建表结构与默认数据，并对旧库做增量迁移。"""
    ensure_db_dir()
    with get_db() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """旧库增量迁移（CREATE TABLE IF NOT EXISTS 不会给已存在的表加列）。"""
    note_cols = {row[1] for row in conn.execute("PRAGMA table_info(notes)")}
    if "tags" not in note_cols:
        conn.execute("ALTER TABLE notes ADD COLUMN tags TEXT")
    if "updated_at" not in note_cols:
        conn.execute("ALTER TABLE notes ADD COLUMN updated_at TIMESTAMP")

    # notes_fts 若是旧的两列结构：重建为三列并全量重建索引
    fts_cols = {row[1] for row in conn.execute("PRAGMA table_info(notes_fts)")}
    if fts_cols and "tags" not in fts_cols:
        conn.execute("DROP TABLE notes_fts")
        conn.execute("CREATE VIRTUAL TABLE notes_fts USING fts5(title, content, tags)")
        rows = conn.execute(
            "SELECT id, title, content, COALESCE(tags, '') AS tags FROM notes"
        ).fetchall()
        for row in rows:
            conn.execute(
                "INSERT INTO notes_fts (rowid, title, content, tags) VALUES (?, ?, ?, ?)",
                (row["id"], segment_cjk(row["title"]),
                 segment_cjk(row["content"]), segment_cjk(row["tags"])),
            )


@contextmanager
def get_db():
    """获取 SQLite 连接，启用外键。"""
    ensure_db_dir()
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def get_db_path() -> Path:
    return settings.db_path
