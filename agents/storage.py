# agents/storage.py
# DAO 层：SQLite 持久化（零依赖，Python 内置 sqlite3）
# 目标：把原 JSON 文件存储迁移到 data/app.db，业务层函数签名保持不变。
# 并发安全：线程本地连接 + WAL + busy_timeout，避免「读-改-写整文件覆盖」丢写。

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "app.db"
REVIEW_TASKS_SEED = BASE_DIR / "data" / "review_tasks.json"

_local = threading.local()

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  type TEXT,
  ask TEXT,
  progress TEXT,
  reply TEXT,
  subjects TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id, id DESC);

CREATE TABLE IF NOT EXISTS user_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT,
  intent TEXT,
  subjects TEXT,
  progress TEXT,
  weak TEXT,
  plan_excerpt TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_user ON user_history(user_id, id DESC);

CREATE TABLE IF NOT EXISTS review_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject TEXT,
  chapter TEXT,
  due_date TEXT,
  priority INTEGER DEFAULT 1,
  done INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS agent_traces (
  trace_id TEXT PRIMARY KEY,
  user_id TEXT,
  payload TEXT,
  created_at TEXT
);
"""


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def connect() -> sqlite3.Connection:
    """获取当前线程的数据库连接（WAL + busy_timeout）"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
    return conn


def query_all(sql: str, params=()) -> list:
    return connect().execute(sql, params).fetchall()


def query_one(sql: str, params=()):
    return connect().execute(sql, params).fetchone()


def execute(sql: str, params=()) -> int:
    """执行写操作（自动提交），返回 lastrowid"""
    conn = connect()
    with conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


def init_db():
    """建表 + 首次从 review_tasks.json 导入种子数据（幂等）"""
    conn = connect()
    with conn:
        conn.executescript(SCHEMA_SQL)
    _seed_review_tasks()


def _seed_review_tasks():
    """review_tasks 表为空时，从 data/review_tasks.json 导入种子"""
    try:
        row = query_one("SELECT COUNT(*) AS c FROM review_tasks")
        if row and row["c"] > 0:
            return
        tasks = []
        if REVIEW_TASKS_SEED.exists():
            data = json.loads(REVIEW_TASKS_SEED.read_text(encoding="utf-8"))
            tasks = data.get("tasks", []) if isinstance(data, dict) else (data or [])
        if not tasks:
            return
        conn = connect()
        with conn:
            conn.executemany(
                "INSERT INTO review_tasks(subject, chapter, due_date, priority, done) "
                "VALUES (?,?,?,?,?)",
                [(t.get("subject", ""), t.get("chapter", ""), t.get("due_date", ""),
                  int(t.get("priority", 1)), 1 if t.get("done") else 0) for t in tasks],
            )
    except Exception as e:
        print(f"[Storage] review_tasks 种子导入失败: {e}")


# 模块导入即保证表结构就绪（幂等）
init_db()