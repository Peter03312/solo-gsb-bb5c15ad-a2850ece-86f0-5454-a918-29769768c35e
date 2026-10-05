"""SQLite 数据访问层。

- 显式事务：写操作在 BEGIN IMMEDIATE 事务中进行，防止答卷与结算并发竞态。
- 所有时间戳统一为服务端 Unix 秒（INTEGER）。
"""

import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("SNIFF_DB_PATH", "/data/sniff.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tests (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT NOT NULL UNIQUE,          -- 负责人管理码
    token_hash         TEXT NOT NULL,                 -- 负责人令牌哈希
    sample_a           TEXT NOT NULL,
    sample_b           TEXT NOT NULL,
    deadline           INTEGER NOT NULL,
    min_valid          INTEGER NOT NULL,
    created_at         INTEGER NOT NULL,
    settled_at         INTEGER,
    valid_count        INTEGER,
    correct_count      INTEGER,
    p_value            REAL,
    distinguishable    INTEGER
);

CREATE TABLE IF NOT EXISTS assignments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id      INTEGER NOT NULL REFERENCES tests(id),
    panelist     TEXT NOT NULL,
    code         TEXT NOT NULL UNIQUE,                -- 评员盲评码
    odd_pos      INTEGER NOT NULL,                    -- 异样杯位置 1/2/3
    odd_sample   TEXT NOT NULL,                       -- 异样杯样本编号（A 或 B）
    answer       INTEGER,                             -- 1/2/3；弃权为 -1；未提交 NULL
    answered_at  INTEGER,
    withdrawn_at INTEGER,                             -- 撤回时刻；非 NULL 即退出有效名单（旧码作废）
    UNIQUE(test_id, panelist)
);

CREATE INDEX IF NOT EXISTS idx_assignments_test ON assignments(test_id);
"""

# 旧库升级语句（列已存在时跳过）
_MIGRATIONS = (
    "ALTER TABLE assignments ADD COLUMN withdrawn_at INTEGER",
)


def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(assignments)").fetchall()
        }
        for stmt in _MIGRATIONS:
            col = stmt.split("ADD COLUMN", 1)[1].strip().split()[0]
            if col not in cols:
                conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def write_tx():
    """立即拿写锁的事务；异常回滚，正常提交。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_conn():
    return connect()
