"""SQLite 连接与建库。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

RAW_TABLES = (
    "raw_policy_scope",
    "raw_binding",
    "raw_deployment",
    "raw_exposure",
    "raw_ingestion_log",
)

CURATED_TABLES = (
    "dim_policy_scope",
    "dim_binding",
    "fact_deployment",
    "fact_exposure",
    "fact_recovery_action",
    "governance_task",
)


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开数仓连接，启用外键并让结果以类字典行返回。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """执行全部 DDL。原始层使用 IF NOT EXISTS，可重复执行。"""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def reset_curated(conn: sqlite3.Connection) -> None:
    """清空策展层，准备从原始层幂等重建。"""
    for table in CURATED_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    # 仅重建策展层部分：重新执行整段脚本，原始层因 IF NOT EXISTS 不受影响。
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
