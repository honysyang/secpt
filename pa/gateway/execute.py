"""动作网关：所有工具执行的唯一入口（BUILD_SPEC §6.5 / §6.5.1；ARCHITECTURE §8 L3）。

顺序固定：存在性 → 工具子集 → 范围 → 风险档 → 去重 → 执行 → 证据落盘 → 审计。
两条硬规矩：
- 范围校验在去重之前——缓存不得越过 scope（S6 追加验收六）；
- 命中去重仍写 action.finished{cached:true, evidence}，审计链不断（裁定 4）。

分层：gateway 只依赖 tools / infra / store / models，不得 import
core / runtime（S6 验收五 grep 把关）。
"""
from __future__ import annotations

import ipaddress
import json
import time
import uuid
from dataclasses import dataclass

from pa.infra import evidence
from pa.infra.fp import VOLATILE_KEYS, fingerprint
from pa.models import MissionState, now
from pa.store.writer import StoreWriter
from pa.tools import REGISTRY
from pa.tools.base import ToolContext

# blocked_reason 封闭枚举（裁定 5+7）：网关只返回这里面的值，不产生自由文本。
# tool_not_found 是文档值 unknown_tool 的同义别名（按裁定保留此名并注明）；
# rate_limited / budget_exceeded v0 网关不产生，留给 S10 预算/限速接线。
BLOCKED_REASONS = ("tool_not_found", "tool_not_allowed", "missing_target",
                   "out_of_scope", "needs_approval", "rate_limited",
                   "budget_exceeded")

FAIL_RETRY_LIMIT = 2          # §6.5.1：失败指纹最多重试 2 次，第 3 次起返回末次结果

_WORKSPACE = "workspace"


@dataclass
class Outcome:
    ok: bool
    blocked_reason: str | None = None       # 只取 BLOCKED_REASONS 中的值
    evidence_id: str | None = None
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    cached: bool = False
    cache_hit_exhausted: bool = False
    duration_ms: int = 0
    risk: str = "low"


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _blocked(state: MissionState, writer: StoreWriter, tool_name: str,
             args: dict, task_id: str, reason: str, started: float) -> Outcome:
    writer.append("action.blocked", "gateway",
                  {"tool": tool_name, "args": args, "reason": reason,
                   "task_id": task_id},
                  f"blk:{fingerprint(tool_name, args)}:{task_id}")
    assert reason in BLOCKED_REASONS          # 封闭枚举，杜绝自由文本
    return Outcome(ok=False, blocked_reason=reason, duration_ms=_ms(started))


def _audit_cache_hit(writer: StoreWriter, tool: str, fp: str, task_id: str,
                     exit_code: int, ev: str | None, started: float) -> None:
    """命中去重仍落 action.finished{cached:true}，审计链不断（裁定 4）。
    idem key 每次命中唯一——每次 gateway 调用都是独立动作，不该互相吞。"""
    writer.append("action.finished", "gateway",
                  {"tool": tool, "fp": fp, "exit": exit_code, "evidence": ev,
                   "cached": True, "task_id": task_id},
                  f"fin:{fp}:{task_id}:hit:{uuid.uuid4().hex[:12]}")


def execute(state: MissionState, writer: StoreWriter, *, tool_name: str,
            args: dict, task_id: str,
            allowed_tools: list[str] | None = None,
            expect: str = "", rationale: str = "") -> Outcome:
    """expect/rationale：worker 决策时的预期观察与理由，随 action.started
    落账（思考链可读；空串兼容旧调用方与旧账本）。"""
    started = time.monotonic()
    spec = state.spec

    # 1) 存在性
    if tool_name not in REGISTRY:
        return _blocked(state, writer, tool_name, args, task_id,
                        "tool_not_found", started)
    tier = REGISTRY[tool_name].risk_tier

    # 2) 工具子集
    if allowed_tools is not None and tool_name not in allowed_tools:
        return _blocked(state, writer, tool_name, args, task_id,
                        "tool_not_allowed", started)

    # 3) 范围（声明驱动，裁定：禁止用"args 里没有 target/url 就跳过"的
    # 参数名嗅探关闭安全检查；fs 这类本地工具在 ToolSpec 显式声明
    # requires_network=False）。范围在去重之前：缓存不得越过 scope。
    if REGISTRY[tool_name].requires_network:
        tgt = _canonical_target(str(args.get("target") or args.get("url") or ""),
                                args.get("port"))
        if not tgt:
            return _blocked(state, writer, tool_name, args, task_id,
                            "missing_target", started)
        if not in_scope(tgt, spec.allowed_scope):
            return _blocked(state, writer, tool_name, args, task_id,
                            "out_of_scope", started)

    # 4) 风险档
    if tier == "critical" and spec.env_class == "production":
        return _blocked(state, writer, tool_name, args, task_id,
                        "needs_approval", started)

    # 5) 去重（§6.5.1 缓存语义）
    fp = fingerprint(tool_name, args)
    if writer.seen_action(fp):
        prev_exit = writer.action_exit(fp)
        if prev_exit == 0:
            # 成功 → 永久缓存（mission 内）
            prev = writer.action_evidence(fp)
            _audit_cache_hit(writer, tool_name, fp, task_id, 0, prev, started)
            return Outcome(ok=True, cached=True, evidence_id=prev,
                           exit_code=0, duration_ms=_ms(started), risk=tier)
        attempts = state.meta.get("fp_attempts", {}).get(fp, 0)
        if attempts >= FAIL_RETRY_LIMIT:
            # 失败已重试 2 次：第 3 次起返回最后一次结果，不再执行
            prev = writer.action_evidence(fp)
            _audit_cache_hit(writer, tool_name, fp, task_id, prev_exit, prev,
                             started)
            last = evidence.get(writer.root, prev) if prev else {}
            return Outcome(ok=False, cached=True, cache_hit_exhausted=True,
                           evidence_id=prev,
                           stdout=str(last.get("stdout", "")),
                           stderr=str(last.get("stderr", "")),
                           exit_code=prev_exit,
                           duration_ms=_ms(started), risk=tier)
        # 失败未满 2 次：允许重试，落到执行

    # 6) 执行
    # 事件 idem key 带执行序号 rN：同指纹失败重试是多次独立执行，
    # 共用同一个 key 会被幂等去重吞掉，审计链断掉
    retry_no = state.meta.get("fp_attempts", {}).get(fp, 0) + 1
    writer.append("action.started", "gateway",
                  {"tool": tool_name, "args": args, "fp": fp,
                   "task_id": task_id, "expect": expect,
                   "rationale": rationale},
                  f"act:{fp}:{task_id}:r{retry_no}")
    ws = writer.root / _WORKSPACE
    ws.mkdir(exist_ok=True)
    ctx = ToolContext(workspace=ws,
                      timeout=float(args.get("timeout", 120)))
    out = REGISTRY[tool_name].fn(args, ctx)

    # 7) 证据落盘（全量，执行者不能筛选）：content.args 完整 args（含
    #    volatile）；meta.semantic_args 剔除 volatile，与指纹同口径（裁定 6）
    semantic = {k: v for k, v in args.items() if k not in VOLATILE_KEYS}
    content = {"tool": tool_name, "args": args, "exit": out.exit_code,
               "stdout": out.stdout.decode("utf-8", "replace"),
               "stderr": out.stderr.decode("utf-8", "replace"),
               "task_id": task_id}
    # 内容里不放时间戳：内容寻址 id 要对同一动作确定性——时间戳在 meta 里
    ev = evidence.put(
        writer.root,
        json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        task_id=task_id,
        extra={"semantic_args": semantic, "truncated": out.truncated,
               "original_bytes": out.original_bytes,
               "truncated_at": out.truncated_at})
    writer.append("evidence.stored", "gateway",
                  {"id": ev, "task_id": task_id}, f"ev:{ev}")
    writer.append("action.finished", "gateway",
                  {"tool": tool_name, "fp": fp, "exit": out.exit_code,
                   "evidence": ev, "task_id": task_id},
                  f"fin:{fp}:{task_id}:r{retry_no}")
    if out.exit_code != 0:
        # §6.5.1 失败计数：与 derive_stats 的账本口径同步累加（裁定 2：
        # 不允许只在内存、重启即丢的行为参数）
        fa = state.meta.setdefault("fp_attempts", {})
        fa[fp] = fa.get(fp, 0) + 1

    # 8) 审计：action.started / .blocked / .finished 已随各步落账
    return Outcome(ok=True, evidence_id=ev,
                   stdout=content["stdout"], stderr=content["stderr"],
                   exit_code=out.exit_code, duration_ms=_ms(started),
                   risk=tier)


def _canonical_target(raw: str, port_arg) -> str:
    """规范化成 host[:port] 供 in_scope 比对：
    - URL（http://host/path、https://host:8443/x）剥 scheme 与路径，无显式
      端口时按 scheme 补默认（80/443）——不规范化会被 partition(":") 切出
      host="http" 而误判越界；
    - probe 类 target/port 分参合并；
    - 其余原样。"""
    from urllib.parse import urlsplit

    s = raw.strip()
    if not s:
        return ""
    if s.startswith(("http://", "https://")):
        u = urlsplit(s)
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "https" else 80)
        return f"{host}:{port}" if host else ""
    if port_arg and ":" not in s:
        return f"{s}:{port_arg}"
    return s


def in_scope(target: str, allowed: list[str]) -> bool:
    """allowed 形如 "10.0.0.0/24:80,443" 或 "app.example.com:443"。
    空 allowed 一律拒绝。"""
    if not allowed:
        return False
    host, _, port = target.partition(":")
    for rule in allowed:
        rhost, _, rports = rule.partition(":")
        if "/" in rhost and ip_in_cidr(host, rhost) and port_matches(port, rports):
            return True
        if rhost == host and port_matches(port, rports):
            return True
    return False


def ip_in_cidr(host: str, cidr: str) -> bool:
    try:
        return ipaddress.ip_address(host) in ipaddress.ip_network(cidr,
                                                                 strict=False)
    except ValueError:
        return False


def port_matches(port: str, rports: str) -> bool:
    if not rports:
        return True
    return port in rports.split(",")
