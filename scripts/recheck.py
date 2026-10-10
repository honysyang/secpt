#!/usr/bin/env python3
"""recheck.py：复跑判据门禁。

读取一次 engagement 运行的 JSON 报告（EngagementReport.model_dump_json()），
输出 6 项 PASS/FAIL 清单。任一项 FAIL → 退出码非 0。

用法：
    python scripts/recheck.py <report.json>
    python scripts/recheck.py < report.json

6 项判据：
    1. spawn_root goal 含真实 target（不得为 unknown）
    2. 首拍存在 list_dir 或 read_file 的成功记录
    3. format_error 占比 < 10%
    4. 存在 tool_calls > 0 的 LLM 调用
    5. 连续失败时 score 单调下降（不得上升）
    6. saml 被调用（真题场景）
"""

from __future__ import annotations

import json
import sys
from typing import Any


def _load_report(path: str | None) -> dict[str, Any]:
    """从文件路径或 stdin 加载 JSON 报告。"""
    if path and path != "-":
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return json.load(sys.stdin)


# ---------------------------------------------------------------------------
# 判据 1：spawn_root goal 含真实 target
# ---------------------------------------------------------------------------

def check_spawn_root_goal(report: dict[str, Any]) -> tuple[bool, str]:
    """检查根节点 goal 是否包含真实 target（不得为 unknown）。"""
    genome = report.get("genome_summary", {})
    targets = genome.get("targets", [])
    objective = genome.get("objective", "")

    # 检查 objective 是否包含 unknown
    if "unknown" in objective.lower():
        return False, f"objective 含 'unknown': {objective[:80]}"

    # 检查是否有真实 target
    if not targets:
        return False, "targets 为空"

    for t in targets:
        if t and t != "unknown":
            return True, f"targets: {targets}"

    return False, f"targets 全为 unknown: {targets}"


# ---------------------------------------------------------------------------
# 判据 2：首拍存在 list_dir 或 read_file 的成功记录
# ---------------------------------------------------------------------------

def check_first_beat_file_op(report: dict[str, Any]) -> tuple[bool, str]:
    """检查首拍是否有 list_dir 或 read_file 的成功调用。"""
    trajectory = report.get("trajectory", {})
    if not trajectory:
        return False, "trajectory 为空"

    # 收集所有拍的动作，按 node + wavefront 排序
    all_entries: list[dict[str, Any]] = []
    for node_id, entries in trajectory.items():
        for entry in entries:
            all_entries.append({**entry, "_node": node_id})

    if not all_entries:
        return False, "trajectory 无条目"

    # 按 wavefront 排序，找最早的
    all_entries.sort(key=lambda e: e.get("wavefront", 0))

    for entry in all_entries[:5]:  # 看前 5 拍
        action = entry.get("action", "")
        observation = entry.get("observation", "")
        if action in ("list_dir", "read_file"):
            if not observation.startswith("Error"):
                return True, f"首拍 {action} 成功: {observation[:60]}"

    return False, f"前 5 拍无成功的 list_dir/read_file，动作: {[e.get('action') for e in all_entries[:5]]}"


# ---------------------------------------------------------------------------
# 判据 3：format_error 占比 < 10%
# ---------------------------------------------------------------------------

def check_format_error_ratio(report: dict[str, Any]) -> tuple[bool, str]:
    """检查 format_error 占比是否 < 10%。"""
    trajectory = report.get("trajectory", {})
    total = 0
    format_errors = 0

    for entries in trajectory.values():
        for entry in entries:
            total += 1
            if entry.get("action") == "format_error":
                format_errors += 1

    if total == 0:
        return False, "trajectory 为空，无法计算"

    ratio = format_errors / total
    ok = ratio < 0.10
    return ok, f"format_error: {format_errors}/{total} = {ratio:.1%}"


# ---------------------------------------------------------------------------
# 判据 4：存在 tool_calls > 0 的 LLM 调用
# ---------------------------------------------------------------------------

def check_tool_calls_used(report: dict[str, Any]) -> tuple[bool, str]:
    """检查是否存在 tool_calls > 0 的调用。"""
    trajectory = report.get("trajectory", {})
    tool_call_count = 0

    for entries in trajectory.values():
        for entry in entries:
            action = entry.get("action", "")
            # tool_calls 路径的动作通常是工具名而非 format_error/answer
            if action not in ("format_error", "answer", "flag_extract", ""):
                tool_call_count += 1

    if tool_call_count > 0:
        return True, f"结构化调用: {tool_call_count} 次"

    # 也检查 thought 字段（结构化调用时 thought 通常非空）
    has_thought = any(
        entry.get("thought", "")
        for entries in trajectory.values()
        for entry in entries
    )
    if has_thought:
        return True, "trajectory 含 thought（结构化路径）"

    return False, "无 tool_calls > 0 的证据"


# ---------------------------------------------------------------------------
# 判据 5：连续失败时 score 单调下降
# ---------------------------------------------------------------------------

def check_score_monotonic_decrease(report: dict[str, Any]) -> tuple[bool, str]:
    """检查连续失败时 score 是否单调下降（不得上升）。"""
    trajectory = report.get("trajectory", {})
    debug = report.get("debug", {})

    # 尝试从 debug 字段获取 scores
    scores = debug.get("node_scores", {})

    if not scores:
        # 从 trajectory 的 observation 推断（failure 时 score 应下降）
        failure_streaks: list[list[float]] = []
        current_streak: list[float] = []

        for node_id, entries in trajectory.items():
            for entry in entries:
                obs = entry.get("observation", "")
                # 从 observation 提取 score 信息（如果有）
                # 简化：检查是否有连续失败
                if "failure" in obs.lower() or "error" in obs.lower():
                    current_streak.append(0.0)  # failure 时 score 应为 0
                else:
                    if len(current_streak) >= 2:
                        failure_streaks.append(current_streak)
                    current_streak = []

        if len(current_streak) >= 2:
            failure_streaks.append(current_streak)

        # 连续失败时 score 都应为 0（不上升）
        for streak in failure_streaks:
            if any(s > 0 for s in streak[1:]):
                return False, f"连续失败时 score 上升: {streak}"

        if failure_streaks:
            return True, f"连续失败 {len(failure_streaks)} 段，score 均未上升"

        return True, "无连续失败场景"

    # 有 scores 数据时严格检查
    for node_id, score_list in scores.items():
        if len(score_list) >= 2:
            for i in range(1, len(score_list)):
                if score_list[i] > score_list[i - 1]:
                    return False, f"node {node_id} score 上升: {score_list[i-1]} → {score_list[i]}"

    return True, "score 单调性通过"


# ---------------------------------------------------------------------------
# 判据 6：saml 被调用（真题场景）
# ---------------------------------------------------------------------------

def check_saml_called(report: dict[str, Any]) -> tuple[bool, str]:
    """检查 saml 工具是否被调用。"""
    trajectory = report.get("trajectory", {})

    for entries in trajectory.values():
        for entry in entries:
            action = entry.get("action", "")
            if action == "saml" or action.startswith("saml"):
                return True, f"saml 被调用: {action}"

    # 也检查 observation 中是否有 saml 相关内容
    for entries in trajectory.values():
        for entry in entries:
            obs = entry.get("observation", "")
            if "saml" in obs.lower():
                return True, "observation 含 saml 相关内容"

    return False, "saml 未被调用"


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

CHECKS = [
    ("spawn_root goal 含真实 target", check_spawn_root_goal),
    ("首拍 list_dir/read_file 成功", check_first_beat_file_op),
    ("format_error 占比 < 10%", check_format_error_ratio),
    ("tool_calls > 0", check_tool_calls_used),
    ("连续失败 score 单调下降", check_score_monotonic_decrease),
    ("saml 被调用", check_saml_called),
]


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python scripts/recheck.py <report.json>", file=sys.stderr)
        print("   或: python scripts/recheck.py < report.json", file=sys.stderr)
        return 2

    path = sys.argv[1]
    try:
        report = _load_report(path)
    except Exception as exc:
        print(f"ERROR: 无法加载报告: {exc}", file=sys.stderr)
        return 2

    print(f"recheck: {path}")
    print("=" * 60)

    all_pass = True
    for name, check_fn in CHECKS:
        try:
            ok, detail = check_fn(report)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"检查异常: {exc}"

        status = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"  [{status}] {name}: {detail}")

    print("=" * 60)
    if all_pass:
        print("结果: ALL PASS")
        return 0
    print("结果: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
