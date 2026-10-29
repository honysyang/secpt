"""两根验收杆（M4-6 §10）：只读统计，纯度量不自动调参。

为什么做成 store 只读函数而不是 loop/worker 行为：度量是观察，观察不动执行——
任何"按杆数自动调参"的冲动都会让度量失真（为好数字而优化），故本模块只有
SELECT、无写口、无副作用，口径钉死在 docstring 与 tests/unit/test_metrics.py。

口径（改口径必须先改测试再改实现，两边同步）：
- 精度杆 verified_precision：verified / (verified + rejected)——findings 终态
  中经 judge 复核判真的比例。claimed 未裁决不进分母（计入会虚低，半场统计
  虚低比虚高诚实——分母口径宁小勿假）；分母为 0 → None（无从统计不编数）。
- 复利杆 experience_hit_rate：SUM(useful_count) / COUNT(*)——经验库平均净票。
  useful_count 是重放命中的净记账（+1 命中 / -1 失配地板 0）：均值 > 0 说明
  经验在二轮回放中产生正向复利；空库 → None。
"""

from __future__ import annotations

import sqlite3

__all__ = ["experience_hit_rate", "render_metrics", "verified_precision"]


def verified_precision(conn: sqlite3.Connection) -> float | None:
    """精度杆（验收可信度）：findings 终态中判真的比例；分母 0 → None。"""
    row = conn.execute(
        "SELECT COUNT(*) FILTER (WHERE verify_state = 'verified') AS pos,"
        " COUNT(*) FILTER (WHERE verify_state = 'rejected') AS neg"
        " FROM findings"
    ).fetchone()
    total = int(row["pos"]) + int(row["neg"]) if row is not None else 0
    if total == 0:
        return None
    return int(row["pos"]) / total


def experience_hit_rate(conn: sqlite3.Connection) -> float | None:
    """复利杆（经验复用率）：experience 平均净票；空库 → None。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(useful_count), 0) AS votes, COUNT(*) AS n FROM experience"
    ).fetchone()
    if row is None or int(row["n"]) == 0:
        return None
    return int(row["votes"]) / int(row["n"])


def render_metrics(conn: sqlite3.Connection) -> str:
    """两杆一行渲染（CLI ``secpt metrics`` 与面板总览共用，None → 暂无不编数）。"""
    precision = verified_precision(conn)
    hit_rate = experience_hit_rate(conn)
    p_text = f"{precision:.1%}" if precision is not None else "暂无数据"
    h_text = f"{hit_rate:.2f} 票/条" if hit_rate is not None else "暂无数据"
    return (
        f"verified 阳性率（精度杆）: {p_text}"
        f" · 经验命中率（复利杆）: {h_text}（只读统计，不自动调参）"
    )
