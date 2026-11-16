"""SQLite 连接与表结构。

设计要点：
- 观测 / 沿革两份输入各自按整份修订（revision）保存，原文与 SHA-256 留档；
- 每次计算（run）固定两份输入修订；结果行与运行记录在同一事务写入，
  失败时整体回滚，绝不开放半份结果；
- is_current 标记当前结果：输入更正或新结果生效时同事务清除。
"""
from __future__ import annotations

import os
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS obs_revision (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uploaded_at TEXT NOT NULL,
    filename    TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    row_count   INTEGER NOT NULL,
    content     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tax_revision (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uploaded_at TEXT NOT NULL,
    filename    TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    row_count   INTEGER NOT NULL,
    content     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT NOT NULL,
    obs_revision_id  INTEGER NOT NULL REFERENCES obs_revision(id),
    tax_revision_id  INTEGER NOT NULL REFERENCES tax_revision(id),
    status           TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    is_current       INTEGER NOT NULL DEFAULT 0,
    note             TEXT NOT NULL DEFAULT '',
    error            TEXT NOT NULL DEFAULT '',
    usable_count     INTEGER NOT NULL DEFAULT 0,
    pending_count    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS run_usable (
    run_id        INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    record_no     TEXT NOT NULL,
    initial_taxon TEXT NOT NULL,
    final_taxon   TEXT NOT NULL,
    quantity      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS run_pending (
    run_id        INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    record_no     TEXT NOT NULL,
    initial_taxon TEXT NOT NULL,
    quantity      INTEGER NOT NULL,
    final_taxons  TEXT NOT NULL,
    reason        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_usable_run ON run_usable(run_id);
CREATE INDEX IF NOT EXISTS idx_run_pending_run ON run_pending(run_id);
"""


def get_db() -> sqlite3.Connection:
    """请求级连接；isolation_level=None 表示由代码显式管理事务。"""
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DB_PATH"],
            timeout=15,  # 写锁等待，避免并发请求偶发 database is locked
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(app) -> None:
    db_path = app.config["DB_PATH"]
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
