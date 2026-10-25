"""工具 manifest → prompt 片段（ARCHITECTURE §2.3：assets/tools.py）。

按 name 排序渲染（约束 B），任何顺序的输入得到逐字节相同的输出。
"""
from __future__ import annotations

import json


def render_tool_manifest(tools: list[dict]) -> str:
    lines = ["# 可用工具（按名称排序）"]
    for t in sorted(tools, key=lambda x: x["name"]):
        req = ",".join(t.get("schema", {}).get("required", []))
        lines.append(
            f"- {t['name']} risk={t['risk_tier']} side_effect={t['side_effect']} "
            f"requires={t['requires']} args_required=[{req}] "
            f"schema={json.dumps(t.get('schema', {}), ensure_ascii=False, sort_keys=True)}")
    return "\n".join(lines)
