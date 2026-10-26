"""自然语言任务理解（智能体侧）：一句话任务 + 附件 → MissionSpec。

层级归属：任务解析是智能体的能力（LLM 参与），不是应用层职责——
应用层（cli）只负责把任务文本与附件原样递进来并呈现确认面板。
P1 只做强确定性抽取：
- targets：IPv4 / CIDR / hostname / URL + 端口列表展开（"的 80 和 443
  端口" → host:80 / host:443）。LLM 不得新增目标——抽不到就报错，不猜；
- scope：由 targets 派生，只收窄；
- acceptance：允许 LLM 起草，但 evidence 必须命中工具线索白名单
  （probe → open/closed，http → status=<code>，shell/fs → key=value），
  命中不了的整条丢弃并警告，全丢弃则用默认验收；
- budget：文本解析（30分钟 / 2小时 / 1h / 45m），默认 15 分钟；
- env_class：出现 生产 / production / 线上 → production（调用方负责
  打印醒目警告）。

附件：文本类材料（任务书、授权范围、urls.txt 等）；内容并入
「理解文本」，目标抽取在其上做（LLM 依旧不得新增目标），
objective 只保留用户任务文本——附件是证据，不是任务声明。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pa.errors import SecptError
from pa.infra.llm import ModelCallError, ModelParseError, parse_json
from pa.models import AcceptanceItem, Budget, FileRef, MissionSpec, PlanSketchItem

# 附件读取上限：单文件 8K 字符足够覆盖一份任务说明书的要点；超出
# 静默截断并警告——不做全文倾倒（token 爆炸）也不拒收（用户体感差）
MAX_ATTACHMENT_CHARS = 8000

# 附件收录上限（文件/目录都收进 workspace，供 fs 工具与确定性扫描用）
MAX_FILE_BYTES = 2_000_000  # 单文件 2MB
MAX_TOTAL_BYTES = 32_000_000  # 总量 32MB
MAX_FILES = 64  # 文件数上限

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
    r"[a-zA-Z]{2,})(?::(\d{1,5}))?" + _RIGHT,
    re.IGNORECASE,
)
_VERSION_CTX_RE = re.compile(r"(?:版本号|版本|version|v)\s*$", re.IGNORECASE)
# "的 80 和 443 端口" / "80、443 端口" / "80,443端口"
# 单个端口也要算（"的80端口"）；中英文逗号、顿号、和、换行都是分隔
_PORT_CLAUSE_RE = re.compile(
    r"(?:的|位于|开放)?\s*(\d{1,5}(?:\s*[、,，和及/]\s*\d{1,5})*)\s*端口"
)
# 后置写法："端口：80 和 443" / "端口 80/443" / "port 80,443"
_PORT_SUFFIX_RE = re.compile(
    r"(?:端口|port)\s*[:：]?\s*(\d{1,5}(?:\s*[、,，和及/]\s*\d{1,5})*)", re.IGNORECASE
)


def _normalize(text: str) -> str:
    """全角 → 半角（１２７．０．０．１：８０８０ → 127.0.0.1:8080），
    全角空白/不间断空白 → 半角空格。抽取前统一过一遍，正则只认半角。"""
    table = {0x3000: " ", 0xA0: " "}
    for cp in range(0xFF01, 0xFF5F):
        table[cp] = chr(cp - 0xFEE0)
    return text.translate(table)


def _valid_ip(s: str) -> bool:
    parts = s.split(".")
    return len(parts) == 4 and all(0 <= int(p) <= 255 and p.isdigit() for p in parts)


def _valid_cidr(s: str) -> bool:
    ip, _, prefix = s.partition("/")
    return _valid_ip(ip) and 0 <= int(prefix) <= 32


def _valid_port(p: str | None) -> bool:
    return p is None or (p.isdigit() and 1 <= int(p) <= 65535)


def extract_targets(text: str) -> list[str]:
    """确定性目标抽取：URL → CIDR → IPv4(+端口) → 域名(+端口)，
    端口从句（前置"的80端口"/后置"端口：80 443"）里的裸端口列表
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

    hosts: list[str] = []  # 未带显式端口的主机（等待端口从句）
    for m in _IP_RE.finditer(work):
        ip, port = m.group(1), m.group(2)
        if not _valid_ip(ip) or not _valid_port(port):
            continue
        if _VERSION_CTX_RE.search(work[: m.start()]):
            continue  # "版本 1.2.3.4"是版本号不是主机
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


# ─────────────────────────── 附件收录与暂存 ───────────────────────────


def collect_files(paths: list[Path] | None) -> tuple[list[tuple[Path, int]], list[str]]:
    """--attachment 路径 → [(源文件, 大小)] + 警告。目录递归收录
    （跳过符号链接）；单文件/总量/数量超限显式警告，不静默丢弃。"""
    files: list[tuple[Path, int]] = []
    warns: list[str] = []
    for p in paths or []:
        if p.is_file():
            size = p.stat().st_size
            if size > MAX_FILE_BYTES:
                warns.append(
                    f"附件 {p.name} 超过单文件上限（{MAX_FILE_BYTES // 1024}KB），跳过"
                )
                continue
            files.append((p, size))
        elif p.is_dir():
            n = 0
            for f in sorted(p.rglob("*")):
                if not f.is_file() or f.is_symlink():
                    continue
                if len(files) >= MAX_FILES:
                    warns.append(
                        f"附件超过 {MAX_FILES} 个文件上限，{p} 其余文件未收录"
                    )
                    break
                size = f.stat().st_size
                if size > MAX_FILE_BYTES:
                    warns.append(
                        f"{f.name} 超过单文件上限（{MAX_FILE_BYTES // 1024}KB），跳过"
                    )
                    continue
                files.append((f, size))
                n += 1
            if n == 0:
                warns.append(f"目录 {p.name} 没有可收录的文件")
        else:
            warns.append(f"附件 {p} 不存在，已忽略")
    total = sum(s for _, s in files)
    if total > MAX_TOTAL_BYTES:
        warns.append(
            f"附件总量 {total // 1024 // 1024}MB 超过上限"
            f"（{MAX_TOTAL_BYTES // 1024 // 1024}MB），仅前若干文件会收录"
        )
    return files, warns


def stage_files(files: list[tuple[Path, int]], dest: Path) -> tuple[list[FileRef], list[str]]:
    """把收录的文件复制进 workspace/_attachments/，返回 FileRef 清单。

    确定性命名：用源文件名；重名（不同目录同名文件）显式跳过并警告——
    名字必须与 build_spec 预测的一致（验收 target 绑定），改写会破坏
    合同。binary 检测：前 8KB 含 NUL 字节。"""
    import hashlib
    import shutil

    dest.mkdir(parents=True, exist_ok=True)
    refs: list[FileRef] = []
    warns: list[str] = []
    total = 0
    for src, size in files:
        if total + size > MAX_TOTAL_BYTES:
            warns.append(f"总量超限，{src.name} 未收录")
            continue
        target = dest / src.name
        if target.exists():
            warns.append(f"重名附件 {src.name} 跳过（确定性命名冲突）")
            continue
        try:
            shutil.copy2(src, target)
        except OSError as e:
            warns.append(f"附件 {src.name} 复制失败（{e}），跳过")
            continue
        raw = target.read_bytes()
        binary = b"\x00" in raw[:8192]
        total += size
        refs.append(
            FileRef(
                name=f"_attachments/{src.name}",
                origin=str(src),
                size=size,
                sha256=hashlib.sha256(raw).hexdigest()[:16],
                binary=binary,
            )
        )
    return refs, warns


# ─────────────────────────── 预算 / 环境 ───────────────────────────

_HOUR_RE = re.compile(r"(\d+)\s*(?:小时|hours?|h)(?![a-zA-Z])", re.IGNORECASE)
_MIN_RE = re.compile(r"(\d+)\s*(?:分钟|mins?|min|m|分)(?![a-zA-Z])", re.IGNORECASE)
DEFAULT_MINUTES = 15


def parse_budget_minutes(text: str | None, default: int = DEFAULT_MINUTES) -> int:
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
    return Budget(
        steps=min(200, max(4, minutes * 2)),
        seconds=minutes * 60,
        tokens=minutes * 12000,
        risk="high" if env_class == "production" else "low",
    )


# ─────────────────────────── 附件 ───────────────────────────


def read_attachments(paths: list[Path]) -> tuple[str, list[str]]:
    """读附件文本，返回 (合并正文, 警告)。每文件截断到
    MAX_ATTACHMENT_CHARS 并注明——附件是大段材料，理解要点即可，
    全量进 prompt 会把预算烧光。"""
    warns: list[str] = []
    pieces: list[str] = []
    for p in paths:
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            warns.append(f"附件 {p} 无法读取（{e}），已忽略")
            continue
        if len(raw) > MAX_ATTACHMENT_CHARS:
            warns.append(
                f"附件 {p.name} 超过 {MAX_ATTACHMENT_CHARS} 字符，已截断（只取开头）"
            )
        pieces.append(f"[附件: {p.name}]\n{raw}")
    return "\n\n".join(pieces), warns


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
# "/robots.txt" 会被域名正则误当主机）。不吞 URL 的 scheme/主机部分：
# "http://" 的两个斜杠分别紧跟 ":" 与 "/"，被排除；"80/tcp" 的斜杠
# 紧跟数字，不能被当成路径 "/tcp"（真跑 acc-3 事故）。中文字符不在
# ASCII 看护集合里，所以「的/admin/」仍可提取。
_PATH_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_.:/-])/[^\s，。；、\"'）)]+")
# URL 内的路径单独提取：上面的 _PATH_TOKEN_RE 故意不吞 host/path 的斜杠
# （否则 host 后紧跟 /admin/ 会被当独立路径），URL 形态由这里兜底。
_URL_PATH_RE = re.compile(r"https?://[^/\s]+(/[^\s，。；、\"'）)]*)", re.IGNORECASE)
# 无斜杠的路径名引用：词后直接跟「路径」的 token 是路径名不是主机，
# 先从目标检测里剔除（否则 "robots.txt 路径" 的 robots.txt 会被域名
# 正则误当主机而整条误杀）。token 与「路径」之间只允许空白，「的」挡在
# 中间的形态不会被吞，保护真正的目标词。
_PATH_WORD_RE = re.compile(r"[^\s，。；、\"'）)]+(?=\s*路径)")
# 验收项文本里的显式端口（"80/tcp"、"443/udp"、"8080 端口" 的补充口径；
# extract_targets 的端口从句只认 "N 端口" 形式）
_ITEM_PORT_RE = re.compile(r"(?<![\d.])(\d{2,5})\s*(?:端口|/tcp|/udp)", re.IGNORECASE)


def _default_acceptance(targets: list[str]) -> list[AcceptanceItem]:
    return [
        AcceptanceItem(
            id="acc-1",
            item="目标端口开放被探测到",
            evidence="open",
            target=_host_of(targets[0]),
        )
    ]


_PROD_WARN = "检测到生产环境关键词：env_class=production，所有动作仍受网关审批"


def gen_acceptance(
    text: str, targets: list[str], llm, attachment_text: str = ""
) -> tuple[list[AcceptanceItem], list[str], str, list[str], list[PlanSketchItem]]:
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
    from pa.core.evidence_kinds import acceptance_evidence_ok

    warnings: list[str] = []
    user = f"任务：{text}\n目标：{', '.join(targets)}"
    if attachment_text:
        user += f"\n附件材料：\n{attachment_text}"
    try:
        raw = llm.complete(
            system=(
                "你是已获书面授权的安全测试项目的计划员。先理解用户"
                "意图，再产出评估计划。只输出 JSON："
                '{"intent": "用户真正想要什么的一句话（评估什么、'
                '担心什么）",'
                ' "dimensions": ["评估维度，如 端口与服务暴露面 / '
                'HTTP 应用识别 / TLS 配置 / 常见敏感路径", ...],'
                ' "tasks": [{"goal": "任务目标，动词开头，一句'
                '话", "kind": "recon / probe / http / web / tls '
                '等", "why": "为什么先做这步（可空）"}, ...],'
                ' "items": [{"item": "具体可验证的安全问题（写明'
                '目标与端口或路径）", "evidence": "证据词"}]}。'
                "tasks 按执行顺序、不超过 6 条，覆盖主要评估维度；"
                "items 不超过 6 条；每条 item 必须包含具体目标与端口"
                "（80/tcp 或 80 端口形式）或路径，禁止笼统表述；"
                "item 表达「该检查被执行且已有结论」，不要写猜测的"
                "具体结果（不写 404/403 等猜测码，端口状态写 "
                "probe.open 即可）；evidence 只能是工具真实产出的"
                "形态：probe.open / probe.closed / http.status.200 "
                "（具体三位状态码，如 http.status.301）/ http.error，"
                "不得编造，同一 evidence 只用一次；"
                "每条验收必须写明它针对哪个目标。"
            ),
            user=user,
            schema=None,
            phase="do",
        )
        obj = parse_json(raw)
        items = obj.get("items") if isinstance(obj, dict) else None
        if not isinstance(items, list):
            raise ModelParseError("items 不是数组")
        intent = (
            str(obj.get("intent") or "").strip()
            if isinstance(obj.get("intent"), str)
            else ""
        )
        dims = [
            str(d).strip()
            for d in (obj.get("dimensions") or [])
            if isinstance(d, str) and d.strip()
        ]
        sketch = [
            PlanSketchItem(
                goal=str(t.get("goal") or "").strip(),
                kind=str(t.get("kind") or "").strip(),
                why=str(t.get("why") or "").strip(),
            )
            for t in (obj.get("tasks") or [])
            if isinstance(t, dict)
            and isinstance(t.get("goal"), str)
            and t["goal"].strip()
        ][:6]
    except (ModelCallError, ModelParseError) as e:
        warnings.append(f"LLM 验收起草失败（{e}），用默认验收")
        return _default_acceptance(targets), warnings, "", [], []

    kept: list[AcceptanceItem] = []
    seen_kinds: set[str] = set()
    for i, it in enumerate(items, 1):
        if (
            not isinstance(it, dict)
            or not isinstance(it.get("item"), str)
            or not isinstance(it.get("evidence"), str)
        ):
            warnings.append(f"第 {i} 条验收格式非法，已丢弃: {it!r}")
            continue
        ev = it["evidence"].strip()
        if not acceptance_evidence_ok(ev):
            warnings.append(
                f"验收证据不属工具产出白名单或为无对象证据类型，已丢弃: {ev!r}"
            )
            continue
        if ev in seen_kinds:
            warnings.append(
                f"第 {i} 条验收与前面重复（同一 evidence {ev!r} 只留一条），"
                f"已丢弃: {it['item'][:40]!r}"
            )
            continue
        # 越界目标检测只看 item 文本；登记过的 evidence kind（如
        # probe.open）是证据词不是目标，路径 token（含 "/robots.txt"
        # 与 "robots.txt 路径" 两种形态）先剔除，再比
        paths = _URL_PATH_RE.findall(it["item"]) + _PATH_TOKEN_RE.findall(it["item"])
        paths = list(dict.fromkeys(paths))
        clean = _PATH_WORD_RE.sub(" ", _PATH_TOKEN_RE.sub(" ", it["item"]))
        item_targets = [t for t in extract_targets(clean) if not _is_kind(t)]
        stray = sorted(t for t in item_targets if not _covered(t, targets))
        if stray:
            warnings.append(
                f"验收项包含范围外目标 {'、'.join(stray)}，已丢弃整条: {it['item']!r}"
            )
            continue
        covered = [t for t in item_targets if _covered(t, targets)]
        if not covered and not paths:
            warnings.append(
                f"验收项未指明具体目标/端口/路径（笼统表述不可验证），"
                f"已丢弃: {it['item']!r}"
            )
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
        kept.append(
            AcceptanceItem(
                id=f"acc-{i}", item=it["item"], evidence=ev, target=bind, path=bind_path
            )
        )
        seen_kinds.add(ev)
    if not kept:
        warnings.append("验收全部未过白名单，用默认验收")
        kept = _default_acceptance(targets)
    return kept, warnings, intent, dims, sketch


def _is_kind(token: str) -> bool:
    """token 是否为登记过的 evidence kind 词（extract_targets 会把
    "probe.open" 之类误当域名/主机抽出来）。"""
    from pa.core.evidence_kinds import TOOL_KIND_CHECKS

    return any(check(token) for check in TOOL_KIND_CHECKS.values())


# ─────────────────────────── 组装 ───────────────────────────


def build_spec(
    text: str,
    *,
    llm,
    budget_text: str | None = None,
    attachments: list[Path] | None = None,
) -> tuple[MissionSpec, list[str]]:
    """一句话任务 + 附件 → (MissionSpec, 警告)。没有可识别目标抛
    SecptError，调用方退出 1 且不得建目录。

    理解口径：targets / budget / env 在「任务文本 + 附件内容」的合并
    文本上抽取（附件可以列范围与端口清单）；objective 只保留任务文本
    本身——附件是证据材料，让它进入账本会让 report 混入整份文件副本。
    """
    warns: list[str] = []
    att_text = ""
    if attachments:
        att_content, att_warns = read_attachments(attachments)
        warns += att_warns
        att_text = att_content
    corpus = text if not att_text else f"{text}\n\n{att_text}"
    files_info, file_warns = collect_files(attachments)
    # file-only 判定看**任务文本**：附件语料里的文件名会被域名正则
    # 抽成"目标"（app.py 长得像 name.tld），不能作为「用户给了目标」
    # 的证据。文本无目标 + 有附件 = 纯文件任务（0day 研究工作流）。
    text_targets = extract_targets(text)
    targets = extract_targets(corpus)
    file_only = not text_targets and bool(files_info)
    if not targets:
        if not files_info:
            raise SecptError(
                "任务描述（含附件）里没有可识别的目标（IPv4 / CIDR / 域名 / URL）"
                "，也没有可分析的附件文件，不做猜测"
            )
        # 纯文件任务：目标 = 附件 basename（确定性命名，与 stage_files
        # 一致——验收 target 绑定依赖这一点）
        targets = [src.name for src, _ in files_info[:1]]
    warns += file_warns
    env_class = detect_env_class(text)
    if env_class == "production":
        warns.append(_PROD_WARN)
    # 预算优先级：显式 --budget > 任务文本（不含附件，附件里的日期数字
    # 易被误读成时长） > 默认 15 分钟
    minutes = (
        parse_budget_minutes(budget_text) if budget_text else parse_budget_minutes(text)
    )
    acceptance, acc_warn, intent, dims, sketch = gen_acceptance(
        text, targets, llm, attachment_text=att_text
    )
    warns += acc_warn
    if file_only:
        # 纯文件任务的默认验收 = file.scan（附件已扫描并有结论）；
        # LLM 起草的 http/probe 类验收在文件任务上永远无法命中。
        # bootstrap 会扫描全部附件，锚定第一个文件的 file.scan 线索
        # 即代表「扫描这一步已完成」。
        acceptance = [
            AcceptanceItem(
                id="acc-1",
                item=(
                    f"全部 {len(files_info)} 个附件完成确定性扫描并有结论"
                    "（密钥/危险 sink/依赖 CVE）"
                ),
                evidence="file.scan",
                target=targets[0],
            )
        ]
        warns.append("纯附件任务：默认验收为 file.scan（附件扫描结论）")
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
    ), warns


def default_dir(text: str) -> Path:
    """默认目录 missions/do-<sha1(text)[:8]>：同一句任务映射同一个家族目录。

    家族目录本身不放账本（账本在 runs/N 子目录，见 cli._do）；返回家族根
    是为了让 cli 层统一在「家族」这一层做 do / resume / report / watch 的
    语义分发。旧版本这里直接返回账本路径，导致第二次 do 同句任务直接
    撞「已有账本」错误退出——改成家族 + runs/N 后同一任务可跑 N 次。"""
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return Path("missions") / f"do-{digest}"
