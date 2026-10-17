"""traffic 插件：录制流量的武器化入口（list / replay / fuzz_param）。

把 traffic 录制库变成可复用攻击面：列出已录请求、确定性回放（可改
method/url/headers/body）、单参数批量差分（marker 命中检测）。模型把创造力
花在差分结果的分析上，逐发构造交给确定性执行。

前置：profile 声明 traffic: true 触发录制（同证据层配置），tools: [traffic]
装配本工具。job_id 必填（录制库按 job 分文件 var/traffic/<job_id>.sqlite）。
"""

from __future__ import annotations

import asyncio
import json

SCHEMA = {
    "type": "object",
    "description": (
        "录制流量武器化。list 列出当前任务录到的 HTTP 交换；replay 重建指定交换"
        "并发送（可覆盖各字段）；fuzz_param 对单参数逐 payload 差分并报告命中。"
    ),
    "properties": {
        "action": {"type": "string", "enum": ["list", "replay", "fuzz_param"]},
        "job_id": {"type": "string", "description": "任务 job_id（录制库按 job 分文件）"},
        "exchange_id": {"type": "integer", "description": "replay/fuzz：交换 id（list 输出里查）"},
        "host_filter": {"type": "string", "description": "list：按 host/关键词过滤"},
        "overrides": {
            "type": "string",
            "description": "replay：JSON 对象，可含 method/url/headers/body",
        },
        "param": {"type": "string", "description": "fuzz：要差分的参数名"},
        "payloads": {
            "type": "string",
            "description": "fuzz：JSON 数组或逗号分隔的 payload 列表（上限 50）",
        },
        "marker": {
            "type": "string",
            "description": "fuzz：响应体命中标记（如回显的特征串），命中置 marker_hit",
        },
        "timeout_s": {"type": "integer", "description": "超时（默认 10s）"},
    },
    "required": ["action", "job_id"],
}


def _parse_payloads(raw: str) -> list[str]:
    raw = str(raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(p) for p in data]
    except json.JSONDecodeError:
        pass
    return [p.strip() for p in raw.split(",") if p.strip()]


async def _traffic(args: dict) -> str:
    from secpt.traffic import replay as _rp

    action = str(args.get("action") or "")
    job_id = str(args.get("job_id") or "")
    timeout_s = max(1, min(60, int(args.get("timeout_s") or 10)))

    if action == "list":
        host = str(args.get("host_filter") or "")
        rows = await asyncio.to_thread(_rp.list_exchanges, job_id, host, 20)
        if not rows:
            return f"（{job_id} 无录制交换" + (f"，过滤 {host!r}）" if host else "）")
        return "\n".join(
            f"#{r['id']} {r['method']} {r['url']} < {r['status']}  body={r['req_body']!r}"
            for r in rows
        )
    if action == "replay":
        ov = {}
        if args.get("overrides"):
            try:
                ov = json.loads(str(args["overrides"]))
            except json.JSONDecodeError as e:
                return f"Error: overrides 不是合法 JSON: {e}"
        r = await _rp.replay(job_id, int(args.get("exchange_id") or 0), ov, timeout_s=timeout_s)
        return json.dumps(r, ensure_ascii=False)[:6000]
    if action == "fuzz_param":
        payloads = _parse_payloads(str(args.get("payloads") or ""))
        r = await _rp.fuzz_param(
            job_id,
            int(args.get("exchange_id") or 0),
            str(args.get("param") or ""),
            payloads,
            marker=str(args.get("marker") or ""),
            timeout_s=timeout_s,
        )
        if "error" in r:
            return f"Error: {r['error']}"
        base = r.get("baseline") or {}
        lines = [
            f"基线: status={base.get('status')} length={base.get('length')} {base.get('error', '')}"
        ]
        for item in r.get("results") or []:
            tag = " ★HIT" if item.get("marker_hit") else ""
            lines.append(
                f"- {item.get('payload')!r}: status={item.get('status')}"
                f" length={item.get('length')}{tag}"
                + (f" err={item.get('error')}" if item.get("error") else "")
            )
        return "\n".join(lines)[:6000]
    return f"Error: 未知 action {action!r}（list/replay/fuzz_param）"


def register() -> dict:
    return {"traffic": (SCHEMA, _traffic)}
