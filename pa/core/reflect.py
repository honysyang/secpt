"""反思层：判据 / 线索抽取 / 进展判定 / 证据校验 / 结果裁决（BUILD_SPEC §7.5-7.6）。

纯计算 + 读视图，无副作用；唯一写事件的路径在 worker（线索落账）。
judge() 对应 CONTROL_PLANE §6.2 判定矩阵：status=done 只是主张，
virtual_success（报 done 但判据未满足）在这里被识别。
"""

from __future__ import annotations

import re

from pa.errors import SecptError
from pa.models import (
    Clue,
    Criterion,
    FailureClass,
    MissionState,
    Task,
    WorkerResult,
)
from pa.infra.log import get_logger

# 覆盖度判据词：「族:条数」，如 http:5 = 本任务范围内 http 族线索至少
# 5 条（按 clue.key 去重——同一 URL 重复请求只算一条）。
_COVERAGE_RE = re.compile(r"^(probe|http|shell|tls|dns):(\d+)$")

# blocked_reason → FailureClass（裁定：AuthZ 只含真正的越界/需审批——
# 混入规划错误会让安全指标虚高、教练误判为对抗行为）：
#   missing_target → Tool（可重试）；out_of_scope / needs_approval → AuthZ（不重试）
#   tool_not_allowed → Plan（任务定义错）；tool_not_found → Model（幻觉出不存在的工具）
#   rate_limited → Env。budget_exceeded 不是失败：不产生 Failure（worker 返回 partial）。
_BLOCKED_CLASS: dict[str, FailureClass] = {
    "missing_target": "Tool",
    "out_of_scope": "AuthZ",
    "needs_approval": "AuthZ",
    "tool_not_allowed": "Plan",
    "tool_not_found": "Model",
    "rate_limited": "Env",
}


def classify_blocked(reason: str) -> FailureClass:
    """未知 reason 不抛（§6.2 裁定 4）：归 Env 并记警告——网关之外的
    新 blocked 原因出现时，宁可保守归类为环境失败，也不能让整个 worker
    崩掉。budget_exceeded 是已知值但走 partial 路径，到这里属编程错误，仍抛。"""
    if reason == "budget_exceeded":
        raise SecptError(
            "budget_exceeded 不是失败：worker 返回 partial，不产生 Failure"
        )
    cls = _BLOCKED_CLASS.get(reason)
    if cls is None:
        get_logger("secpt.reflect").warning("未知 blocked_reason %r，归类 Env", reason)
        return "Env"
    return cls


def criteria_met(
    clues: list[Clue], criteria: list[Criterion], task_id: str | None = None
) -> bool:
    """判据语义（联调裁定）：判据表达的是「该问题已被探测并有了结论」，
    不是「现实吻合模型事先的猜测」——同族证据互为印证（probe.open 的
    判据被 probe.closed 线索满足 = 端口状态已确认；http.status.404 的
    判据被 http.status.200 满足 = 状态已观测）。真实联调中模型猜
    「443 关闭」而实际开放、猜「robots.txt 返回 404」而实际 200，
    精确匹配让任务永远不可能成功，空烧预算。

    覆盖度判据（防最小功）：evidence 词形如「http:5」「tls:1」表示
    本任务范围内该族线索 ≥ N 条——「枚举常见敏感路径」这类目标一条
    GET / 喂不饱（联调实测：枚举任务的判据写成 http.status.200，worker
    一次根路径请求就交差，敏感路径一个没探）。

    task_id 给定（judge/worker  live 路径）时只比本任务线索与未归属
    线索（source_task=None）：防止 A 任务的探测结果替 B 任务交差。
    互斥同族的判据仍折叠成「命中其一即可」（requirement_groups）。"""
    from pa.core.evidence_kinds import family_of, requirements_met

    scoped = (
        [c for c in clues if c.source_task is None or c.source_task == task_id]
        if task_id is not None
        else clues
    )

    def hit(e: str) -> bool:
        # http.meta 要求确实是结构化响应元数据，而不是任意一个 http 状态码；
        # 否则“安全响应头已采集”会被一次 200 交差。
        if e == "http.meta":
            return any(
                c.kind.startswith("http.") and bool(c.meta.get("headers"))
                for c in scoped
            )
        for c in scoped:
            if e in (c.kind, c.key, str(c.value)):
                return True
            fe, fc = family_of(e), family_of(c.kind)
            if fe is not None and fe == fc:
                return True
        return False

    words: list[str] = []
    coverage: list[tuple[str, int]] = []
    for c in criteria:
        if not c.required:
            continue
        m = _COVERAGE_RE.fullmatch(c.evidence.strip())
        if m:
            coverage.append((m.group(1), int(m.group(2))))
        else:
            words.append(c.evidence)
    if not requirements_met(words, hit):
        return False
    for fam, n in coverage:
        have = len({c.key for c in scoped if family_of(c.kind) == fam})
        if have < n:
            return False
    return True


def judge(task: Task, result: WorkerResult, state: MissionState) -> str:
    """CONTROL_PLANE §6.2 判定矩阵。返回 success | virtual_success |
    failure | false_failure | blocked | partial。"""
    met = criteria_met(state.clues, task.success_criteria, task_id=task.id)
    if result.status == "done" and met:
        return "success"
    if result.status == "done" and not met:
        return "virtual_success"  # ★ 报完成但判据未满足
    if result.status == "failed" and not met:
        return "failure"
    if result.status == "failed" and met:
        return "false_failure"
    if result.status == "blocked":
        return "blocked"
    return "partial"


def validate_evidence(view, refs: list[str], task_id: str | None = None) -> list[str]:
    """证据溯源校验（裁定 3 签名：view 是只读 StoreView）。

    返回不合法 ref 列表：查无此证、内容校验不过、或 task_id 不匹配
    （跨任务引用，v0 也拦）。空列表 = 全部合法。"""
    bad: list[str] = []
    for r in refs:
        try:
            meta = view.evidence_meta(r)
        except SecptError:
            bad.append(r)
            continue
        if not view.evidence_verified(r):
            bad.append(r)
            continue
        if task_id is not None and meta.get("task_id") not in (None, task_id):
            bad.append(r)
    return bad


def is_progress(
    out,
    expect: str,
    *,
    new_clue: bool,
    fp: str,
    history: dict[str, str],
    body_sig: str | None = None,
    seen_content: set[str] | None = None,
) -> bool:
    """进展判定（§872 最小实现 + 联调裁定的两个修正）：

    - 新 clue（(kind,value) 不在动作前基线）→ 进展；
    - 否则执行成功（out.ok；http 的 Outcome.exit_code 是 200，不能拿
      exit_code==0 当成功）且有输出时：
      - 提供 body_sig/seen_content（worker 主路径）→ **内容新颖**算进展：
        catch-all 站点同 body 的换路径重试不算（早停保留），
        真实异构响应（不同正文/不同工具输出）算进展；
      - 未提供 → 旧口径：同指纹历史输出不同算进展。
    """
    if new_clue:
        return True
    ok = bool(getattr(out, "ok", out.exit_code == 0))
    if ok and out.stdout:
        if body_sig is not None and seen_content is not None:
            return body_sig not in seen_content
        return history.get(fp) != out.stdout
    return False


def extract_clues(tool: str, args: dict, out) -> list[Clue]:
    """从工具产出规则化抽线索（v0 最小集，确定性）：
    - probe: open/closed 一条，key = target:port
    - tls: 握手成功 → tls.cert 一条（value = 证书字段 dict）；失败 →
      tls.error 一条（value = 错误摘要），key = target:port
    - http: 状态码一条，key = url
    - shell/fs: stdout 里 "key=value" 形式的行（最多 10 条）

    kind 一律经 pa/core/evidence_kinds 的构造函数生成（点分枚举），
    产出必须命中该工具在 TOOL_KIND_CHECKS 登记的白名单（契约测试）。
    """
    from pa.core.evidence_kinds import (
        dns_kind,
        http_kind,
        probe_kind,
        shell_kind,
        tls_kind,
    )
    from urllib.parse import urlsplit

    stdout = (
        out.stdout.decode("utf-8", "replace")
        if isinstance(out.stdout, bytes)
        else str(out.stdout)
    )
    clues: list[Clue] = []
    if tool == "probe":
        kind = probe_kind(out.exit_code)
        state = kind.split(".", 1)[1]  # value 与 kind 同源，不另拼
        clues.append(
            Clue(
                kind=kind,
                key=f"{args.get('target')}:{args.get('port')}",
                value=state,
                evidence_id=out.evidence_id,
                confidence=0.9 if state == "open" else 0.7,
                target=str(args.get("target")),
            )
        )
    elif tool == "tls":
        # 证书/握手摘要一条：key = host:port，value = 字段 dict——
        # 过期天数、verify_error 等都是评估数据（见 tls_kind 注释）
        if out.exit_code == 0:
            fields = {}
            for line in stdout.splitlines():
                if "=" in line and line[0].isalnum():
                    k, _, v = line.partition("=")
                    fields[k.strip()] = v.strip()
            clues.append(
                Clue(
                    kind=tls_kind(0),
                    key=f"{args.get('target')}:{args.get('port')}",
                    value=fields,
                    evidence_id=out.evidence_id,
                    confidence=0.9,
                    target=str(args.get("target")),
                )
            )
        else:
            stderr = (
                out.stderr.decode("utf-8", "replace")
                if isinstance(out.stderr, bytes)
                else str(out.stderr)
            )
            clues.append(
                Clue(
                    kind=tls_kind(1),
                    key=f"{args.get('target')}:{args.get('port')}",
                    value=stderr.strip()[:200],
                    evidence_id=out.evidence_id,
                    confidence=0.7,
                    target=str(args.get("target")),
                )
            )
    elif tool == "dns":
        host = str(args.get("target") or "")
        if out.exit_code == 0:
            ips = []
            for line in stdout.splitlines():
                if line.startswith("ip="):
                    ips.append(line.partition("=")[2].strip())
            clues.append(
                Clue(
                    kind=dns_kind(0),
                    key=host,
                    value={"ips": [x for x in ips if x]},
                    evidence_id=out.evidence_id,
                    target=host,
                    confidence=0.9,
                    meta=dict(getattr(out, "meta", None) or {}),
                )
            )
        else:
            err = (
                out.stderr.decode("utf-8", "replace")
                if isinstance(out.stderr, bytes)
                else str(out.stderr)
            )
            clues.append(
                Clue(
                    kind=dns_kind(1),
                    key=host,
                    value=err.strip()[:200],
                    evidence_id=out.evidence_id,
                    target=host,
                    confidence=0.7,
                )
            )
    elif tool == "http":
        url = str(args.get("url"))
        u = urlsplit(url)
        clues.append(
            Clue(
                kind=http_kind(out.exit_code),
                key=url,
                value=f"status={out.exit_code}",
                evidence_id=out.evidence_id,
                target=u.hostname or url,
                path=u.path or "/",
                meta=dict(getattr(out, "meta", None) or {}),
            )
        )
    else:
        kind = shell_kind(out.exit_code)
        for line in stdout.splitlines():
            if "=" in line and line[0].isalnum() and len(clues) < 10:
                k, _, v = line.partition("=")
                clues.append(
                    Clue(
                        kind=kind,
                        key=k.strip(),
                        value=v.strip(),
                        evidence_id=out.evidence_id,
                    )
                )
    return clues
