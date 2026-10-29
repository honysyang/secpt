"""事实层入口：sqlite 连接与 schema 版本推进。

模块设计动机（为什么不是一坨散 SQL）：
- 单文件单库、每表带 task_id 是需求文档钉死的约束；连接参数（外键、WAL）属于
  "每个连接都必须一样"的部分，收敛到 :func:`connect` 一处。
- schema 演进走 baseline（schema.sql）+ 增量 migrations 的双段式：baseline 建六表
  后永远不再修改，结构变更全部进 migrations/，靠 PRAGMA user_version 断言进度——
  这让重跑 migration 天然幂等，也让"老库新代码"或"新库旧代码"都能显式报错。
"""

from __future__ import annotations

import importlib.resources
import sqlite3
from pathlib import Path

from secpt.errors import LedgerError

# baseline（schema.sql，version=1）之后的增量迁移：后续 W 卡按序追加。
# 每个条目 = (迁移后的 schema_version, 迁移文件名)。
_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (2, "0002_graph_tables.sql"),
    (3, "0003_experience_table.sql"),
    (4, "0004_observations_table.sql"),
    (5, "0005_findings_actor.sql"),
    (6, "0006_findings_sweep_count.sql"),
)

_STORE_DIR = "store"


def connect(path: str | Path) -> sqlite3.Connection:
    """打开一个已对齐 schema 的 sqlite 连接（调用方负责关闭）。

    Args:
        path: 数据库文件路径；``:memory:`` 用于测试。

    Returns:
        已开启外键与 WAL 的连接，schema 版本已推进到最新。

    Raises:
        LedgerError: schema 推进失败（文件损坏/迁移脚本错误）。
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":  # WAL 只对文件库有意义；内存库加不减都无所谓
            conn.execute("PRAGMA journal_mode = WAL")
        ensure_schema(conn)
    except sqlite3.Error as exc:
        conn.close()
        raise LedgerError("打开事实层失败", context={"path": str(path)}) from exc
    return conn


def _read_baseline_sql() -> str:
    src = importlib.resources.files("secpt") / _STORE_DIR / "schema.sql"
    return src.read_text(encoding="utf-8")


def _read_migration_sql(filename: str) -> str:
    src = importlib.resources.files("secpt") / _STORE_DIR / "migrations" / filename
    return src.read_text(encoding="utf-8")


def ensure_schema(conn: sqlite3.Connection) -> int:
    """把连接所指向的库推进到最新 schema 版本（幂等，可重复调用）。

    Returns:
        推进完成后的 schema_version。

    Raises:
        LedgerError: 迁移脚本执行失败，或库版本倒挂（老库撞新迁移语义）。
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current == 0:
        conn.executescript(_read_baseline_sql())
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        current = 1

    for target_version, filename in _MIGRATIONS:
        if current >= target_version:
            continue
        try:
            conn.executescript(_read_migration_sql(filename))
            # user_version 一条一条推进：失败即中止，绝不跳版本。
            conn.execute(f"PRAGMA user_version = {target_version}")
            conn.commit()
            current = target_version
        except sqlite3.Error as exc:
            conn.rollback()
            raise LedgerError(
                "schema 迁移失败",
                context={"target_version": target_version, "migration": filename},
            ) from exc
    return current


def schema_version(conn: sqlite3.Connection) -> int:
    """读取当前库的 schema_version（测试与 evidence 命令使用）。"""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


__all__ = ["connect", "ensure_schema", "schema_version"]
