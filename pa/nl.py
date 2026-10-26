"""secpt do 的自然语言门面（Part 2）：一句话任务 → MissionSpec。

P1 只做强确定性抽取：
- targets：IPv4 / CIDR / hostname / URL + 端口列表展开（“的 80 和 443
  端口” → host:80 / host:443）。LLM 不得新增目标——抽不到就报错，不猜；
- scope：由 targets 派生，只收窄；
- acceptance：允许 LLM 起草，但 evidence 必须命中工具线索白名单
  （probe → open/closed，http → status=<code>，shell/fs → key=value），
  命中不了的整条丢弃并警告，全丢弃则用默认验收；
- budget：文本解析（30分钟 / 2小时 / 1h / 45m），默认 15 分钟；
- env_class：出现 生产 / production / 线上 → production（调用方负责
  打印醒目警告）。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from pa.errors import SecptError
from pa.infra.llm import ModelCallError, ModelParseError, parse_json
from pa.models import AcceptanceItem, Budget, MissionSpec, PlanSketchItem

# ─────────────────────────── 目标抽取 ───────────────────────────

# 注意：不能用 \b——Unicode 模式下中文字符也是 \w，「描」与「a」之间
# 没有词边界，「扫描aimohu.com」会整体匹配失败。改用 ASCII 字符看护。
_LEFT = r"(?<![A-Za-z0-9.-])"
_RIGHT = r"(?![A-Za-z0-9.-])"

_URL_RE = re.compile(r"https?://[^\s，。；、\"'']+")
_CIDR_RE = re.compile(_LEFT + r"(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})" + _RIGHT)
# 非法 CIDR（/33）不降级当裸 IP；版本号（v1.2.3.4 / 版本 1.2.3.4）不是目标
_BAD_CIDR_RE = re.compile(_LEFT + r"\d{1,3}(?:\.\d{1,3}){3}/\d{1,3}" + _RIGHT)
_IP_RE = re.compile(_LEFT + r"(\d{1,3}(?:\.\d{1,3}){3})(?::(\d{1,5}))?" + _RIGHT)
_HOST_RE = re.compile(
    _LEFT + r"((?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z]{2,})(?::(\d{1,5}))?" + _RIGHT, re.IGNORECASE)
_VERSION_CTX_RE = re.compile(r"(?:版本号|版本|version|v)\s*$", re.IGNORECASE)
# “的 80 和 443 端口” / “80、443 端口” / “80,443端口”
# 单个端口也要算（“的80端口”）；中英文逗号、顿号、和、换行都是分隔
_PORT_CLAUSE_RE = re.compile(
    r"(?:的|位于|开放)?\s*(\d{1,5}(?:\s*[、,，和及/]\s*\d{1,5})*)\s*端口")
# 后置写法：“端口：80 和 443” / “端口 80/443” / “port 80,443”
_PORT_SUFFIX_RE = re.compile(
    r"(?:端口|port)\s*[:：]?\s*(\d{1,5}(?:\s*[、,，和及/]\s*\d{1,5})*)",
    re.IGNORECASE)


def _normalize(text: str) -> str:
    """全角 → 半角（１２７．０．０．１：８０８０ → 127.0.0.1:8080），
    全角空白/不间断空白 → 半角空格。抽取前统一过一遍，正则只认半角。"""
    table = {0x3000: " ", 0xA0: " "}
    for cp in range(0xFF01, 0xFF5F):
        table[cp] = chr(cp - 0xFEE0)
    return text.translate(table)


def _valid_ip(s: str) -> bool:
    parts = s.split(".")
    return len(parts) == 4 and all(0 <= int(p) <= 255 and p.isdigit()
                                   for p in parts)


def _valid_cidr(s: str) -> bool:
    ip, _, prefix = s.partition("/")
    return _valid_ip(ip) and 0 <= int(prefix) <= 32


def _valid_port(p: str | None) -> bool:
    return p is None or (p.isdigit() and 1 <= int(p) <= 65535)


def extract_targets(text: str) -> list[str]:
    """确定性目标抽取：URL → CIDR → IPv4(+端口) → 域名(+端口)，
    端口从句（前置“的80端口”/后置“端口：80 443”）里的裸端口列表
    展开到未带端口的主机上。保序去重。输入先全角归一化。"""
    spans: list[tuple[int, int]] = []
    out: list[str] = []
    seen: set[str] = set()

    def add(t: str) -> None:
        if t not in seen:
            seen.add(t)
            out.append(t)

    work = _normalize(text)
    # URL 先吃掉，避免其中的 IP/域名被二次匹配
    for m in _URL_RE.finditer(work):
        add(m.group(0))
        spans.append(m.span())
    for a, b in reversed(spans):
        work = work[:a] + " " * (b - a) + work[b:]
    spans.clear()

    for m in _CIDR_RE.finditer(work):
        if _valid_cidr(m.group(1)):
            add(m.group(1))
            spans.append(m.span())
    for a, b in reversed(spans):
        work = work[:a] + " " * (b - a) + work[b:]
    spans.clear()
    # 非法 CIDR（如 /33）整体作废，不降级当裸 IP 误扫
    for m in _BAD_CIDR_RE.finditer(work):
        spans.append(m.span())
    for a, b in reversed(spans):
        work = work[:a] + " " * (b - a) + work[b:]
    spans.clear()

    hosts: list[str] = []           # 未带显式端口的主机（等待端口从句）
    for m in _IP_RE.finditer(work):
        ip, port = m.group(1), m.group(2)
        if not _valid_ip(ip) or not _valid_port(port):
            continue
        if _VERSION_CTX_RE.search(work[:m.start()]):
            continue                # “版本 1.2.3.4”是版本号不是主机
        add(f"{ip}:{port}" if port else ip)
        hosts.append(f"{ip}:{port}" if port else ip)
        spans.append(m.span())
    for a, b in reversed(spans):
        work = work[:a] + " " * (b - a) + work[b:]
    spans.clear()

    for m in _HOST_RE.finditer(work):
        host, port = m.group(1), m.group(2)
        if not _valid_port(port):
            continue
        add(f"{host}:{port}" if port else host)
        hosts.append(f"{host}:{port}" if port else host)
        spans.append(m.span())
    for a, b in reversed(spans):
        work = work[:a] + " " * (b - a) + work[b:]

    clause_ports: list[str] = []
    for rx in (_PORT_CLAUSE_RE, _PORT_SUFFIX_RE):
        for m in rx.finditer(work):
            clause_ports += re.findall(r"\d{1,5}", m.group(1))
    clause_ports = [p for p in dict.fromkeys(clause_ports) if _valid_port(p)]
    bare = [h for h in hosts if ":" not in h]
    if clause_ports and bare:
        # 端口从句把裸主机展开成 host:port 列表：裸形态被替换，不再单列
        for h in bare:
            seen.discard(h)
        out[:] = [t for t in out if t not in bare]
        for h in bare:
            for p in clause_ports:
                add(f"{h}:{p}")
    return out


def scope_from_targets(targets: list[str]) -> list[str]:
    """scope 由 targets 派生、只收窄：URL 归一到 host:port，其余原样。"""
    out: list[str] = []
    for t in targets:
        if t.startswith(("http://", "https://")):
            m = re.match(r"https?://([^/?#]+)", t)
            scope = m.group(1) if m else t
        else:
            scope = t
        if scope not in out:
            out.append(scope)
    return out


# ─────────────────────────── 预算 / 环境 ───────────────────────────

_HOUR_RE = re.compile(r"(\d+)\s*(?:小时|hours?|h)(?![a-zA-Z])", re.IGNORECASE)
_MIN_RE = re.compile(r"(\d+)\s*(?:分钟|mins?|min|m|分)(?![a-zA-Z])",
                     re.IGNORECASE)
DEFAULT_MINUTES = 15


def parse_budget_minutes(text: str | None, default: int = DEFAULT_MINUTES
                         ) -> int:
    """30分钟 / 2小时 / 1h / 45m → 分钟数；缺省 default。"""
    if not text:
        return default
    m = _HOUR_RE.search(text)
    if m:
        return int(m.group(1)) * 60
    m = _MIN_RE.search(text)
    if m:
        return int(m.group(1))
    return default


def detect_env_class(text: str) -> str:
    low = text.lower()
    if any(k in low for k in ("生产", "production", "线上")):
        return "production"
    return "lab"


def budget_for(minutes: int, env_class: str) -> Budget:
    # tokens 按真实联调校准：一轮（planner 引导 + 4 个 worker 派发）
    # 实测 5 万+ token（usage 实计），旧的 4000/分钟连 3 轮都撑不到；
    # 12000/分钟 ≈ 15 分钟任务可跑 8-12 轮。tokens 先于 steps 收口。
    return Budget(steps=min(200, max(4, minutes * 2)),
                  seconds=minutes * 60,
                  tokens=minutes * 12000,
                  risk="high" if env_class == "production" else "low")


# ─────────────────────────── 验收 ───────────────────────────

# acceptance 白名单以 pa/core/evidence_kinds 的登记为准（与 clue.kind
# 同一个构造函数口径）；旧形态（open/closed、status=NNN、key=value）
# 保留兼容，见该模块注释


def _covered(target: str, targets: list[str]) -> bool:
    """验收项里出现的目标必须被已抽取目标覆盖（同地址或同主机）。
    双方都先归一到主机（剥 scheme 与端口）：item 文本里的
    http://aimohu.com/ 与目标 aimohu.com 同主机，算覆盖。"""
    host = _host_of(target)
    for x in targets:
        if target == x or host == _host_of(x):
            return True
    return False


def _host_of(target: str) -> str:
    """验收绑定的对象形态：只取主机（剥 scheme 与端口），与 clue.target
    同口径（extract_clues 的 probe/http 线索 target 都是主机）。"""
    t = target
    if t.startswith(("http://", "https://")):
        m = re.match(r"https?://([^/?#:]+)", t)
        return m.group(1) if m else t
    return t.split(":", 1)[0]


# 验收项文本里的路径 token（绑 path 用；也从目标检测里剔除——
# "/robots.txt" 会被域名正则误当主机）。不吞 URL 的 scheme 部分：
# "http://" 的两个斜杠分别紧跟 ":" 与 "/"，被排除。
_PATH_TOKEN_RE = re.compile(r"(?<!:)(?<!/)/[^\s，。；、\"'）)]+")
# 无斜杠的路径名引用：词后直接跟「路径」的 token 是路径名不是主机，
# 先从目标检测里剔除（否则 "robots.txt 路径" 的 robots.txt 会被域名
# 正则误当主机而整条误杀）。token 与「路径」之间只允许空白，「的」挡在
# 中间的形态不会被吞，保护真正的目标词。
_PATH_WORD_RE = re.compile(r"[^\s，。；、\"'）)]+(?=\s*路径)")
# 验收项文本里的显式端口（"80/tcp"、"443/udp"、"8080 端口" 的补充口径；
# extract_targets 的端口从句只认 "N 端口" 形式）
_ITEM_PORT_RE = re.compile(r"(?<![\d.])(\d{2,5})\s*(?:端口|/tcp|/udp)",
                           re.IGNORECASE)


def _default_acceptance(targets: list[str]) -> list[AcceptanceItem]:
    return [AcceptanceItem(id="acc-1", item="目标端口开放被探测到",
                           evidence="open", target=_host_of(targets[0]))]


_PROD_WARN = "检测到生产环境关键词：env_class=production，所有动作仍受网关审批"


def gen_acceptance(text: str, targets: list[str], llm) -> tuple[
        list[AcceptanceItem], list[str], str, list[str], list[PlanSketchItem]]:
    """LLM 以安全专家身份做意图理解 + 评估维度 + 规划草案 + 验收起草
    → 过滤 + 对象绑定。

    返回 (保留项, 警告, 意图摘要, 评估维度, 规划草案)。任何模型失败都
    不致命：丢弃并落默认验收（意图/维度/草案为空）。

    过滤规则（每条都防「死板枚举」）：
    - 白名单 + 非无对象证据类型（shell.ok/shell.error/fs.result 禁用）；
    - 越界目标整条丢弃；
    - 具体性：验收项必须写明具体目标/端口/路径——「对目标域名进行
      端口探测」这种笼统句直接丢弃，逼起草器写出可验证的安全问题；
    - 去重：同一 evidence 只留第一条，防止按工具种类逐一枚举凑数。
    对象绑定：item 文本里提到唯一被覆盖目标就绑它，否则绑主目标。
    """
    from pa.core.evidence_kinds import TOOL_KIND_CHECKS, acceptance_evidence_ok

    warnings: list[str] = []
    try:
        raw = llm.complete(
            system=("你是已获书面授权的安全测试项目的计划员。先理解用户"
                    "意图，再产出评估计划。只输出 JSON："
                    "{\"intent\": \"用户真正想要什么的一句话（评估什么、"
                    "担心什么）\","
                    " \"dimensions\": [\"评估维度，如 端口与服务暴露面 / "
                    "HTTP 应用识别 / TLS 配置 / 常见敏感路径\", ...],"
                    " \"tasks\": [{\"goal\": \"任务目标，动词开头，一句"
                    "话\", \"kind\": \"recon / probe / http / web / tls "
                    "等\", \"why\": \"为什么先做这步（可空）\"}, ...],"
                    " \"items\": [{\"item\": \"具体可验证的安全问题（写明"
                    "目标与端口或路径）\", \"evidence\": \"证据词\"}]}。"
                    "tasks 按执行顺序、不超过 6 条，覆盖主要评估维度；"
                    "items 不超过 6 条；每条 item 必须包含具体目标与端口"
                    "（80/tcp 或 80 端口形式）或路径，禁止笼统表述；"
                    "item 表达「该检查被执行且已有结论」，不要写猜测的"
                    "具体结果（不写 404/403 等猜测码，端口状态写 "
                    "probe.open 即可）；evidence 只能是工具真实产出的"
                    "形态：probe.open / probe.closed / http.status.200 "
                    "（具体三位状态码，如 http.status.301）/ http.error，"
                    "不得编造，同一 evidence 只用一次；"
                    "每条验收必须写明它针对哪个目标。"),
            user=f"任务：{text}\n目标：{', '.join(targets)}",
            schema=None, phase="do")
        obj = parse_json(raw)
        items = obj.get("items") if isinstance(obj, dict) else None
        if not isinstance(items, list):
            raise ModelParseError("items 不是数组")
        intent = str(obj.get("intent") or "").strip() \
            if isinstance(obj.get("intent"), str) else ""
        dims = [str(d).strip() for d in (obj.get("dimensions") or [])
                if isinstance(d, str) and d.strip()]
        sketch = [
            PlanSketchItem(
                goal=str(t.get("goal") or "").strip(),
                kind=str(t.get("kind") or "").strip(),
                why=str(t.get("why") or "").strip())
            for t in (obj.get("tasks") or [])
            if isinstance(t, dict)
            and isinstance(t.get("goal"), str) and t["goal"].strip()
        ][:6]
    except (ModelCallError, ModelParseError) as e:
        warnings.append(f"LLM 验收起草失败（{e}），用默认验收")
        return _default_acceptance(targets), warnings, "", [], []

    kept: list[AcceptanceItem] = []
    seen_kinds: set[str] = set()
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict) or not isinstance(it.get("item"), str) \
                or not isinstance(it.get("evidence"), str):
            warnings.append(f"第 {i} 条验收格式非法，已丢弃: {it!r}")
            continue
        ev = it["evidence"].strip()
        if not acceptance_evidence_ok(ev):
            warnings.append(
                f"验收证据不属工具产出白名单或为无对象证据类型，"
                f"已丢弃: {ev!r}")
            continue
        if ev in seen_kinds:
            warnings.append(
                f"第 {i} 条验收与前面重复（同一 evidence {ev!r} 只留一条），"
                f"已丢弃: {it['item'][:40]!r}")
            continue
        # 越界目标检测只看 item 文本；登记过的 evidence kind（如
        # probe.open）是证据词不是目标，路径 token（含 "/robots.txt"
        # 与 "robots.txt 路径" 两种形态）先剔除，再比
        is_kind = lambda s: any(check(s) for check in
                                TOOL_KIND_CHECKS.values())
        paths = _PATH_TOKEN_RE.findall(it["item"])
        clean = _PATH_WORD_RE.sub(
            " ", _PATH_TOKEN_RE.sub(" ", it["item"]))
        item_targets = [t for t in extract_targets(clean)
                        if not is_kind(t)]
        stray = sorted(t for t in item_targets if not _covered(t, targets))
        if stray:
            warnings.append(
                f"验收项包含范围外目标 {'、'.join(stray)}，已丢弃整条: "
                f"{it['item']!r}")
            continue
        covered = [t for t in item_targets if _covered(t, targets)]
        if not covered and not paths:
            warnings.append(
                f"验收项未指明具体目标/端口/路径（笼统表述不可验证），"
                f"已丢弃: {it['item']!r}")
            continue
        bind = _host_of(covered[0]) if covered else _host_of(targets[0])
        # 验收项写明端口（80/tcp、8080 端口）时绑定到 host:port——
        # probe 线索 key 即 host:port，匹配时端口必须吻合
        m_port = _ITEM_PORT_RE.search(it["item"])
        if m_port and ":" not in bind:
            bind = f"{bind}:{m_port.group(1)}"
        # path 只对 http 族证据有意义：probe 的一次调用不带路径，
        # 绑了反而永不命中
        bind_path = paths[0] if paths and ev.startswith("http.") else None
        kept.append(AcceptanceItem(id=f"acc-{i}", item=it["item"],
                                   evidence=ev, target=bind,
                                   path=bind_path))
        seen_kinds.add(ev)
    if not kept:
        warnings.append("验收全部未过白名单，用默认验收")
        kept = _default_acceptance(targets)
    return kept, warnings, intent, dims, sketch


# ─────────────────────────── 组装 ───────────────────────────

def build_spec(text: str, *, llm, budget_text: str | None = None) -> tuple[
        MissionSpec, list[str]]:
    """一句话任务 → (MissionSpec, 警告)。没有可识别目标抛 SecptError，
    调用方退出 1 且不得建目录。"""
    targets = extract_targets(text)
    if not targets:
        raise SecptError(
            "任务描述里没有可识别的目标（IPv4 / CIDR / 域名 / URL），不做猜测")
    env_class = detect_env_class(text)
    warnings: list[str] = []
    if env_class == "production":
        warnings.append(_PROD_WARN)
    # 预算优先级：显式 --budget > 任务文本 > 默认 15 分钟
    minutes = parse_budget_minutes(budget_text) if budget_text \
        else parse_budget_minutes(text)
    acceptance, acc_warn, intent, dims, sketch = gen_acceptance(
        text, targets, llm)
    warnings += acc_warn
    return MissionSpec(
        objective=text,
        targets=targets,
        acceptance=acceptance,
        env_class=env_class,
        budget=budget_for(minutes, env_class),
        allowed_scope=scope_from_targets(targets),
        intent=intent,
        dimensions=dims,
        plan_sketch=sketch,
    ), warnings


def default_dir(text: str) -> Path:
    """默认目录 missions/do-<sha1(text)[:8]>：同一句任务映射同一路径，
    确定性、可预期。"""
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return Path("missions") / f"do-{digest}"
