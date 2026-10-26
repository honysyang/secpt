"""附件确定性扫描（0day 研究工作流的引擎侧第一遍）。

定位与边界（诚实口径）：
- **能做的**：源码/配置/依赖清单里的已知形态风险——硬编码密钥、危险
  sink（eval/os.system/shell=True/pickle/yaml.load/verify=False）、
  弱哈希、依赖版本与本地 advisory 库（SECPT_ADVISORIES，OSV 导出格式
  兼容）的 1day 匹配。这是 0day 研究的筛选层，不是漏洞验证器；
- **不能做的**：新形态逻辑漏洞的自动发现——那属于 worker 源码审计
  任务（type=code，fs.read/grep），LLM 结论走 Finish.findings 提案；
- **产出纪律**：规则命中 → evidence 落盘 + finding.proposed（走 JEV
  裁决，同 secrules 管道）+ cred 线索（报告脱敏）；密钥内容在
  evidence/reproduce 里一律截断脱敏——证据链要能复核，但不能把秘密
  复制进台账正文。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from pa.infra.log import get_logger
from pa.models import Clue, FindingProposal, FileRef

logger = get_logger("secpt.filerules")

MAX_SCAN_BYTES = 512_000  # 单文件扫描上限（超出截断，警告进 value）
MAX_HITS_PER_RULE = 3  # 单文件单规则最多报 3 处（防刷屏）
MAX_FINDINGS_PER_FILE = 8

# ── 密钥规则（命中 → high/med finding + cred 线索，值脱敏） ──────


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: re.Pattern
    severity: str
    secret: bool  # True → 额外落 cred 线索（报告自动脱敏）
    hint: str  # claim 描述


_SECRET_RULES: tuple[_Rule, ...] = (
    _Rule(
        "aws_access_key",
        re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
        "high",
        True,
        "疑似 AWS 访问密钥（AKIA…）",
    ),
    _Rule(
        "private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
        "high",
        False,
        "疑似私钥文件/内嵌私钥块",
    ),
    _Rule(
        "github_token",
        re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36})\b"),
        "high",
        True,
        "疑似 GitHub Personal Access Token",
    ),
    _Rule(
        "slack_token",
        re.compile(r"\b(xox[baprs]-[A-Za-z0-9\-]{10,})\b"),
        "med",
        True,
        "疑似 Slack Token",
    ),
    _Rule(
        "generic_credential",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*"
            r"[\"']([^\"']{8,})[\"']"
        ),
        "med",
        True,
        "疑似硬编码凭据字面量",
    ),
)

# 占位符形态不算泄漏（示例代码/模板）
_PLACEHOLDER = re.compile(
    r"(?i)(\$\{|\{\{|<[^>]+>|xxx+|change[_-]?me|your[_-]?|example|placeholder|"
    r"dummy|test[_-]?key|<token>)"
)

# ── 危险 sink 规则（0day 源码审计的第一遍信号） ──────────────────

_DANGEROUS_SINKS: tuple[_Rule, ...] = (
    _Rule(
        "python_eval",
        re.compile(r"\beval\s*\("),
        "med",
        False,
        "eval() 动态执行——若输入可控即代码注入",
    ),
    _Rule(
        "python_exec",
        re.compile(r"\bexec\s*\("),
        "med",
        False,
        "exec() 动态执行——若输入可控即代码注入",
    ),
    _Rule(
        "subprocess_shell",
        re.compile(r"subprocess\.\w+\s*\([^)]*shell\s*=\s*True", re.DOTALL),
        "high",
        False,
        "subprocess shell=True——命令注入风险",
    ),
    _Rule(
        "os_system",
        re.compile(r"\bos\.system\s*\("),
        "med",
        False,
        "os.system()——命令注入风险",
    ),
    _Rule(
        "pickle_deser",
        re.compile(r"\bpickle\.loads?\s*\("),
        "med",
        False,
        "pickle 反序列化——不可信数据即 RCE",
    ),
    _Rule(
        "yaml_load",
        re.compile(r"yaml\.load\s*\((?![^)]*Loader\s*=)"),
        "med",
        False,
        "yaml.load 未指定 SafeLoader——反序列化风险",
    ),
    _Rule(
        "tls_verify_false",
        re.compile(r"verify\s*=\s*False\b"),
        "high",
        False,
        "TLS 校验被关闭（verify=False）——中间人风险",
    ),
    _Rule(
        "sql_fstring",
        re.compile(r"""f["'](?!.*\bwhere\b)?\s*(select|insert|update|delete)\b""", re.I),
        "med",
        False,
        "f-string 拼 SQL——注入风险",
    ),
    _Rule(
        "weak_hash",
        re.compile(r"\b(hashlib\.)?(md5|sha1)\s*\("),
        "low",
        False,
        "MD5/SHA1——弱哈希（用于口令存储即 high 风险，请人工复核）",
    ),
    _Rule(
        "flask_debug",
        re.compile(r"\bdebug\s*=\s*True\b"),
        "low",
        False,
        "debug=True——调试模式（Werkzeug 调试器 RCE 历史）",
    ),
)


def _redact(line: str, rule: _Rule) -> str:
    """命中行脱敏：secret 规则只留行首 24 字符，其余留 160。"""
    flat = " ".join(line.split())
    return flat[:24] + "…" if rule.secret else flat[:160]


@dataclass(frozen=True)
class Hit:
    rule: str
    severity: str
    line_no: int
    snippet: str  # 已脱敏
    secret: bool
    claim: str


def scan_text(text: str) -> list[Hit]:
    """单文件文本 → 命中列表。每个规则每文件最多 MAX_HITS_PER_RULE 处，
    占位符形态跳过（示例代码不算泄漏）。"""
    hits: list[Hit] = []
    counts: dict[str, int] = {}
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip():
            continue
        for rule in (*_SECRET_RULES, *_DANGEROUS_SINKS):
            if counts.get(rule.name, 0) >= MAX_HITS_PER_RULE:
                continue
            m = rule.pattern.search(line)
            if m is None:
                continue
            matched = m.group(0)
            # 占位符过滤只用于 generic 凭据（示例代码的 your-token/
            # changeme 不算泄漏）；结构化密钥（AKIA 前缀/ghp_ 前缀）
            # 本身就是信号，"EXAMPLE" 这种文档词不豁免。
            if rule.name == "generic_credential" and _PLACEHOLDER.search(matched):
                continue
            counts[rule.name] = counts.get(rule.name, 0) + 1
            hits.append(
                Hit(
                    rule=rule.name,
                    severity=rule.severity,
                    line_no=i,
                    snippet=_redact(line, rule),
                    secret=rule.secret,
                    claim=rule.hint,
                )
            )
            if len(hits) >= MAX_FINDINGS_PER_FILE:
                return hits
    return hits


# ── 依赖清单解析 + 本地 advisory 匹配（1day 研究的自动化部分） ────

_REQ_LINE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*(==|>=|~=)\s*([A-Za-z0-9_.\-+]+)")
_PKG_JSON_VER = re.compile(r"^[\^~>=< ]*([0-9][A-Za-z0-9.\-+]*)")
_GO_REQUIRE = re.compile(r"^\s*(\S+)\s+(v[0-9][\w.\-+]*)")


def parse_deps(file_name: str, text: str) -> list[tuple[str, str, str]]:
    """依赖清单 → [(ecosystem, package, version)]。

    支持 requirements.txt / pyproject 简式（pypi）、package.json（npm）、
    go.mod（go）。只取可判定的版本形态（==/floor/固定版本）。"""
    out: list[tuple[str, str, str]] = []
    low = file_name.lower()
    if low.endswith((".txt",)) and "requirement" in low or low == "pyproject.toml":
        for raw in text.splitlines():
            m = _REQ_LINE.match(raw.strip())
            if m:
                out.append(("pypi", m.group(1).lower(), m.group(3)))
    elif low.endswith("package.json"):
        try:
            obj = json.loads(text)
        except ValueError:
            return out
        deps = {}
        for key in ("dependencies", "devDependencies"):
            deps.update(obj.get(key) or {})
        for name, ver in deps.items():
            m = _PKG_JSON_VER.match(str(ver))
            if m:
                out.append(("npm", str(name).lower(), m.group(1)))
    elif low == "go.mod":
        for raw in text.splitlines():
            m = _GO_REQUIRE.match(raw)
            if m and not raw.strip().startswith("//"):
                out.append(("go", m.group(1).lower(), m.group(2).lstrip("v")))
    return out


def _ver_tuple(v: str) -> tuple:
    parts = []
    for chunk in re.split(r"[.\-+]", str(v)):
        digits = re.sub(r"\D", "", chunk)
        parts.append(int(digits) if digits else 0)
    return tuple(parts[:6])


def match_advisories(
    entries: list[tuple[str, str, str]], advisories: list[dict]
) -> list[dict]:
    """(eco, pkg, ver) × advisory 库 → 命中。

    advisory 形态：{"ecosystem","name","introduced"(可空),"fixed","id",
    "severity"}——兼容 OSV 导出的最小字段集。语义：introduced <= v < fixed；
    introduced 缺失 = v < fixed。版本比较为数值段元组（非数字段归零），
    复杂版本语法（range/specifier）不在覆盖范围——宁漏报不误报。"""
    hits: list[dict] = []
    for eco, pkg, ver in entries:
        vt = _ver_tuple(ver)
        for adv in advisories:
            if str(adv.get("ecosystem") or "").lower() != eco:
                continue
            if str(adv.get("name") or "").lower() != pkg:
                continue
            fixed = adv.get("fixed")
            if not fixed:
                continue
            introduced = adv.get("introduced")
            if introduced and _ver_tuple(introduced) > vt:
                continue
            if vt >= _ver_tuple(fixed):
                continue
            hits.append({**adv, "version": ver})
    return hits


def load_advisories() -> list[dict]:
    """SECPT_ADVISORIES → JSONL advisory 库；缺席/损坏 = 能力缺席
    （与 MCP 同款保守回退），坏行跳过并告警。"""
    from pa import envs

    path = envs.get(envs.ADVISORIES)
    if not path:
        return []
    out: list[dict] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as e:
        logger.warning("advisory 库读取失败: %s", e)
        return []
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            logger.warning("advisory 库第 %d 行不是合法 JSON，跳过", i)
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def scan_attachment(root: Path, ref: FileRef) -> tuple[list[Hit], list[dict], bool]:
    """单个附件 → (命中, 依赖, 是否二进制)。文件缺失/读取失败按
    空结果处理（警告走调用方 value 字段）。"""
    path = root / "workspace" / ref.name
    try:
        raw = path.read_bytes()
    except OSError as e:
        logger.warning("附件 %s 读取失败: %s", ref.name, e)
        return [], [], ref.binary
    if ref.binary or b"\x00" in raw[:8192]:
        return [], [], True
    text = raw[:MAX_SCAN_BYTES].decode("utf-8", "replace")
    deps = parse_deps(ref.name, text)
    advisories = load_advisories()
    dep_hits = match_advisories(deps, advisories) if deps else []
    return scan_text(text), dep_hits, False


def finding_for(ref: FileRef, hit: Hit, evidence_id: str | None) -> FindingProposal:
    """命中 → finding 提案（证据链 + 反证条件，走 JEV 管道）。"""
    return FindingProposal(
        claim=f"{ref.name}:{hit.line_no} {hit.claim}（规则 {hit.rule}）",
        severity=hit.severity,  # type: ignore[arg-type]
        evidence_refs=[evidence_id] if evidence_id else [],
        reproduce=f"查看 {ref.name} 第 {hit.line_no} 行（fs.read）",
        falsify_if=[
            "该行为测试样例/文档示例，不在运行路径",
            "输入源已在上游校验（需人工确认调用链）",
        ],
        asset_id=ref.name,
        origin=["file-scan"],
    )


def advisory_finding(
    ref: FileRef, adv: dict, evidence_id: str | None
) -> FindingProposal:
    ver = adv.get("version", "?")
    sev = str(adv.get("severity") or "med")
    return FindingProposal(
        claim=(
            f"{ref.name} 依赖 {adv.get('name')}@{ver} 命中已知漏洞 "
            f"{adv.get('id')}（修复于 {adv.get('fixed')}）"
        ),
        severity=sev if sev in ("info", "low", "med", "high", "critical") else "med",
        evidence_refs=[evidence_id] if evidence_id else [],
        reproduce=f"核对 {ref.name} 中 {adv.get('name')} 的版本约束",
        falsify_if=[
            "依赖未被实际 import/调用（仅声明）",
            "漏洞触发条件不满足（需人工复核利用路径）",
        ],
        asset_id=ref.name,
        origin=["file-scan"],
    )


def clue_for(ref: FileRef, hits: list[Hit], dep_count: int, binary: bool) -> Clue:
    """file.scan 线索：附件已扫描并有结论（纯文件任务的验收锚点）。

    key/target 用 basename（与 build_spec 预测的验收 target 一致），
    完整 workspace 路径只在 FileRef.name 里留给 fs 工具。"""
    return Clue(
        kind="file.scan",
        key=ref.basename,
        value={
            "binary": binary,
            "secrets": sum(1 for h in hits if h.secret),
            "sinks": sum(1 for h in hits if not h.secret),
            "deps": dep_count,
        },
        target=ref.basename,
        source_task=None,
        confidence=0.9,
    )
