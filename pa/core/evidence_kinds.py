"""证据类型点分枚举（补充裁定）：clue.kind 与 acceptance.evidence 的
唯一构造函数出口——禁止调用点字符串拼装。

- probe → probe.open / probe.closed（exit_code 0 / 非 0）
- http  → http.status.<code>（2xx/3xx）/ http.error（4xx/5xx、网络错误；
  http 工具的 exit_code 即状态码，网络错误约定 exit 1）
- shell / fs → shell.ok（exit 0）/ shell.error（非 0）

TOOL_KIND_CHECKS 是每个工具登记的白名单（谓词形式，http.status.* 是
开族），契约测试据此校验 extract_clues 的每个产出。acceptance 白名单
evidence_ok 以登记白名单为准，另保留旧形态（open/closed、status=NNN、
key=value）以兼容既有 mission.json 与验收文本。
"""
from __future__ import annotations

import re
from collections.abc import Callable

# ─────────────────────────── 构造函数 ───────────────────────────


def probe_kind(exit_code: int) -> str:
    return "probe.open" if exit_code == 0 else "probe.closed"


def http_kind(exit_code: int) -> str:
    if 200 <= exit_code < 400:
        return f"http.status.{exit_code}"
    return "http.error"


def shell_kind(exit_code: int) -> str:
    return "shell.ok" if exit_code == 0 else "shell.error"


# ─────────────────────────── 工具白名单 ───────────────────────────

_HTTP_STATUS_RE = re.compile(r"^http\.status\.\d{3}$")


def _probe_ok(kind: str) -> bool:
    return kind in (probe_kind(0), probe_kind(1))


def _http_ok(kind: str) -> bool:
    return kind == http_kind(1) or bool(_HTTP_STATUS_RE.fullmatch(kind))


def _shell_ok(kind: str) -> bool:
    return kind in (shell_kind(0), shell_kind(1))


TOOL_KIND_CHECKS: dict[str, Callable[[str], bool]] = {
    "probe": _probe_ok,
    "http": _http_ok,
    "shell": _shell_ok,
    "fs": _shell_ok,        # fs 走同一 shell 家族口径（exit 0/非 0）
}


def tool_kind_ok(tool: str, kind: str) -> bool:
    check = TOOL_KIND_CHECKS.get(tool)
    return check(kind) if check is not None else False


# ─────────────────────────── acceptance 白名单 ───────────────────────────

_LEGACY_KEYWORDS = ("open", "closed")           # 旧 clue value 形态
_LEGACY_STATUS_RE = re.compile(r"^status=\d{3}$")
_LEGACY_KV_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}=[^=\s]{1,256}$")


def evidence_ok(evidence: str) -> bool:
    """acceptance.evidence 准入：命中任一工具登记的 kind，或旧形态
    （兼容既有 mission.json；新代码一律用构造函数生成）。"""
    e = evidence.strip()
    if any(check(e) for check in TOOL_KIND_CHECKS.values()):
        return True
    return (e in _LEGACY_KEYWORDS
            or bool(_LEGACY_STATUS_RE.fullmatch(e))
            or bool(_LEGACY_KV_RE.fullmatch(e)))


# 无对象证据类型（P1-2）：产出不绑定具体目标（主机/路径）的 kind 不得
# 作验收——否则一条 shell.ok 就能给任意目标交差，对象绑定形同虚设。
OBJECTLESS_KINDS: frozenset[str] = frozenset(
    {shell_kind(0), shell_kind(1), "fs.result"})


def acceptance_evidence_ok(evidence: str) -> bool:
    """验收起草侧的完整准入：白名单 AND 非无对象类型。"""
    return evidence_ok(evidence) and evidence.strip() not in OBJECTLESS_KINDS


# ─────────────────────────── 互斥证据组 ───────────────────────────
# 同一工具族的一次调用只产出一个结果：probe 非开即关、http 要么有状态
# 码要么出错、shell 要么 ok 要么 error。LLM 起草判据/验收时惯于把互斥
# 结果并列（「确认开放」+「确认关闭」）——语义是「命中其一即确认」，
# 不是「两个都要」。同族折叠成一个槽位，族与族之间仍是 AND。


def _family(kind: str) -> str | None:
    for fam in ("probe", "http", "shell"):
        if kind.startswith(fam + "."):
            return fam
    return None


# 公开别名：同族匹配的判定点（criteria / acceptance 共用同一口径）
family_of = _family


def requirement_groups(reqs: list[str]) -> list[list[str]]:
    """把需求列表按互斥族折叠成槽位：同族多个 kind 合并为一组
    （命中其一即可），非同族的每个 kind 独占一个槽位（必须命中）。"""
    groups: list[list[str]] = []
    for r in reqs:
        fam = _family(r)
        for g in groups:
            if fam is not None and _family(g[0]) == fam:
                if r not in g:
                    g.append(r)
                break
        else:
            groups.append([r])
    return groups


def requirements_met(reqs: list[str], hit) -> bool:
    """hit(evidence) -> bool 线索是否命中该证据词。每个槽位至少命中
    一个；空需求视为已满足。"""
    for g in requirement_groups(reqs):
        if not any(hit(e) for e in g):
            return False
    return True


def acceptance_hit(c, a) -> bool:
    """验收三要素匹配（P1-2 + 联调裁定）——判定唯一出口，主循环收口
    与 report 渲染共用同一口径（修复前 report 用旧的宽松比对，同一
    账本出现「收口说 3/6、报告说 0/6」的自相矛盾）：

    - evidence 精确命中线索 kind/key/value，或**同族命中**（判据语义与
      criteria_met 一致：验收项表达「该问题已探测并有结论」，不惩罚
      模型对具体结果的错误猜测——「robots.txt 返回 404」的验收在实际
      返回 200 时也必须可达成）；
    - target 主机必须一致；probe 族验收写明端口（target 带 :port）时，
      必须命中同一 host:port 的线索（clue.key 即 host:port）——否则
      443 的开放会替 80 端口交差；
    - path 仅在验收项声明了路径时参与比对。
    禁止只比 kind（一条 probe.open 不能替另一个主机交差）。"""
    fa = family_of(a.evidence)
    if a.evidence not in (c.kind, c.key, str(c.value)):
        if fa is None or fa != family_of(c.kind):
            return False
    if a.target.split(":", 1)[0] != (c.target or ""):
        return False
    if fa == "probe" and ":" in a.target and c.key != a.target:
        return False
    if a.path is not None and a.path != c.path:
        return False
    return True
