"""SQLite 连接与表结构。

设计要点：
- 观测 / 沿革两份输入各自按整份修订（revision）保存，原文与 SHA-256 留档；
- 每次计算（run）固定两份输入修订；结果行与运行记录在同一事务写入，
  失败时整体回滚，绝不开放半份结果；
- is_current 标记当前结果：输入更正或新结果生效时同事务清除；
- app_state 单行表保存 generation（库版本号）：整库恢复成功后递增，
  用于并发围栏——恢复开始前已打开的写事务在提交前复查 generation，
  发现已变化即整体回滚，保证恢复前已开始的上传 / 计算（含失败记录）
  不会在恢复成功后写回。
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
    tax_revision_id  INTEGER NOT NULL,
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

CREATE TABLE IF NOT EXISTS app_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    generation INTEGER NOT NULL
);

INSERT OR IGNORE INTO app_state (id, generation) VALUES (1, 1);
"""

# 业务数据表（备份 / 恢复覆盖的范围，不含内部表 app_state）
BACKUP_TABLES = (
    "obs_revision",
    "tax_revision",
    "run",
    "run_usable",
    "run_pending",
)


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


def current_generation(db: sqlite3.Connection) -> int:
    return db.execute(
        "SELECT generation FROM app_state WHERE id = 1").fetchone()["generation"]


class GenerationMismatchError(Exception):
    """写事务提交前发现库已被整库恢复替换（generation 已变化）。

    触发后调用方必须回滚：恢复前已开始的上传 / 计算（含失败记录）
    一律不得写回恢复后的库。
    """


def begin_snapshot(db: sqlite3.Connection) -> int:
    """请求开始时打开快照读事务并捕获 generation。

    DEFERRED 事务在第一条读语句时才真正获取快照，整个请求期间
    读到的都是同一个已提交状态（完整旧库或完整备份，不会读到恢复中间态）。
    """
    db.execute("BEGIN DEFERRED")
    return current_generation(db)


def end_snapshot(db: sqlite3.Connection) -> None:
    """请求结束时清理仍未提交的快照读事务。"""
    if db.in_transaction:
        db.execute("COMMIT")


def begin_write(db: sqlite3.Connection, snapshot_generation: int) -> None:
    """把快照读事务升级为写事务并做 generation 围栏检查。

    先结束读事务，再 BEGIN IMMEDIATE 排队拿写锁；拿到后复查 generation：
    若期间发生过整库恢复，则抛 GenerationMismatchError，调用方不得写入。
    """
    if db.in_transaction:
        db.execute("COMMIT")
    db.execute("BEGIN IMMEDIATE")
    if current_generation(db) != snapshot_generation:
        raise GenerationMismatchError
