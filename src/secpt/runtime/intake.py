"""intake：把自然语言 objective 解析成「题面全文 + 已验活的 targets」。

流程（能确定性做的就别劳 LLM）：
a. 正则抽 URL/host:port/域名 → 候选 target
b. 本地路径 → list_dir（确定性）→ 找题面文件全读进来
c. 题面交 LLM 抽取 JSON：{"access": [...], "internal_addr": [...], "attach": [...]}
d. access 作 targets；internal_addr 只写 notes，绝不作为扫描目标
e. preflight：对每个 target 做 ≤5s 探活，只保留 reachable 的
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from secpt.core.llm.base import BaseLLM, LLMMessage
from secpt.core.tools.base import Registry

logger = structlog.get_logger(__name__)

# 允许扫描的路径前缀（与 ReadFileTool/ListDirTool 一致）
_ALLOWED_PREFIXES = ("/home/kali/challenges", "/home/kali/secpt", "/tmp")


@dataclass
class IntakeResult:
    """intake 阶段输出。"""

    objective: str = ""          # 带题面全文的 objective
    targets: list[str] = field(default_factory=list)  # 已验活的 targets
    notes: list[str] = field(default_factory=list)    # 备注（含 internal_addr）


# ---------------------------------------------------------------------------
# 确定性提取
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s'\")\]]+", re.IGNORECASE)
# 匹配 "http 10.0.0.1 8080" 或 "https 10.0.0.1 8080"（无 :// 的简化写法）
_HTTP_SPACE_RE = re.compile(
    r"\b(https?)\s+((?:\d{1,3}\.){3}\d{1,3})\s+(\d{1,5})\b",
    re.IGNORECASE,
)
_HOSTPORT_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b")
_DOMAIN_RE = re.compile(r"\b(?:[a-zA-Z0-9-]+\.)+(?:com|net|org|io|cn|edu|gov)\b", re.IGNORECASE)
_PATH_RE = re.compile(r"(/home/kali/[\w./-]+|/tmp/[\w./-]+)")


def _extract_urls(text: str) -> list[str]:
    """从文本中提取 http(s)://host:port 形式的 URL。

    也识别 "http 10.0.0.1 8080"（空格分隔、无 ://）的简化写法，归一化为 http://10.0.0.1:8080。
    """
    urls = _URL_RE.findall(text)

    # 识别 "http 10.0.0.1 8080" 形式
    for scheme, ip, port in _HTTP_SPACE_RE.findall(text):
        normalized = f"{scheme.lower()}://{ip}:{port}"
        if normalized not in urls:
            urls.append(normalized)

    # 去重保序
    seen: set[str] = set()
    result: list[str] = []
    for u in urls:
        u = u.rstrip("/.,;")
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def _extract_hostports(text: str) -> list[str]:
    """从文本中提取 host:port（无 scheme）。"""
    matches = _HOSTPORT_RE.findall(text)
    seen: set[str] = set()
    result: list[str] = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


def _extract_domains(text: str) -> list[str]:
    """从文本中提取域名。"""
    matches = _DOMAIN_RE.findall(text)
    seen: set[str] = set()
    result: list[str] = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


def _extract_paths(text: str) -> list[str]:
    """从文本中提取本地路径。"""
    matches = _PATH_RE.findall(text)
    seen: set[str] = set()
    result: list[str] = []
    for m in matches:
        m = m.rstrip("/.,;")
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


def _is_allowed_path(path: str) -> bool:
    """检查路径是否在允许前缀内。"""
    resolved = str(Path(path).resolve())
    return any(resolved.startswith(p) for p in _ALLOWED_PREFIXES)


# ---------------------------------------------------------------------------
# 题面文件发现（确定性）
# ---------------------------------------------------------------------------

# 题面文件特征：.md / README* / *test* / *题目* / *challenge*
_DOC_PATTERNS = (".md", ".txt", ".rst")


def _looks_like_doc(filename: str) -> bool:
    """判断文件名是否像题面文件。"""
    lower = filename.lower()
    if lower.endswith(_DOC_PATTERNS):
        return True
    if "readme" in lower:
        return True
    if "test" in lower or "题目" in filename or "challenge" in lower:
        return True
    return False


async def _find_and_read_docs(
    tools: Registry,
    directory: str,
    max_chars: int = 6000,
) -> str:
    """在目录中找题面文件并读取内容（确定性）。"""
    list_tool = tools.get("list_dir")
    if list_tool is None:
        return ""

    listing = await list_tool.run(directory)
    if listing.startswith("Error"):
        return listing

    # 从清单中找题面文件
    docs: list[str] = []
    for line in listing.split("\n"):
        line = line.strip()
        if not line or line.startswith("目录 "):
            continue
        # 解析文件名（去掉大小后缀）
        name = line.split()[0] if line.split() else ""
        name = name.rstrip("/")  # 去掉目录标记
        if name and _looks_like_doc(name):
            docs.append(str(Path(directory) / name))

    if not docs:
        return ""

    # 读取所有题面文件
    read_tool = tools.get("read_file")
    if read_tool is None:
        return ""

    contents: list[str] = []
    total = 0
    for doc_path in docs:
        if total >= max_chars:
            break
        content = await read_tool.run(doc_path)
        if content.startswith("Error"):
            continue
        remaining = max_chars - total
        if len(content) > remaining:
            content = content[:remaining] + "\n... [truncated]"
        contents.append(f"--- {doc_path} ---\n{content}")
        total += len(content)

    return "\n\n".join(contents)


# ---------------------------------------------------------------------------
# LLM 题面抽取
# ---------------------------------------------------------------------------

_INTAKE_PROMPT = """\
从以下渗透测试题目描述中抽取信息，只输出 JSON（不要输出其他内容）：
{{"access": ["http://host:port 或可访问的入口地址"], "internal_addr": ["内网地址，不作为扫描目标"], "attach": ["附件路径"]}}

题目描述：
{description}
"""


async def _llm_extract(
    llm: BaseLLM,
    description: str,
) -> dict[str, list[str]]:
    """让 LLM 从题面中抽取 access/internal_addr/attach。"""
    prompt = _INTAKE_PROMPT.format(description=description[:6000])
    messages = [
        LLMMessage(role="system", content="你是信息抽取助手。只输出 JSON，不要输出其他内容。"),
        LLMMessage(role="user", content=prompt),
    ]

    try:
        resp = await llm.acompletion(messages)
        content = (resp.content or "").strip()
        # 剥围栏
        content = re.sub(r"^```\w*\s*", "", content)
        content = re.sub(r"```\s*$", "", content)
        data = json.loads(content)
        if isinstance(data, dict):
            return {
                "access": data.get("access", []) if isinstance(data.get("access"), list) else [],
                "internal_addr": data.get("internal_addr", []) if isinstance(data.get("internal_addr"), list) else [],
                "attach": data.get("attach", []) if isinstance(data.get("attach"), list) else [],
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("intake.llm_extract_error", error=str(exc))

    return {"access": [], "internal_addr": [], "attach": []}


# ---------------------------------------------------------------------------
# preflight 探活
# ---------------------------------------------------------------------------

async def _preflight(target: str) -> bool:
    """对 target 做 ≤5s 探活。返回是否 reachable。"""
    import asyncio

    from secpt.runtime.subprocess import run_whitelisted

    # 解析 target
    if target.startswith("http://") or target.startswith("https://"):
        # HTTP 探活：curl -I -m 5
        try:
            result = await run_whitelisted("curl", ["-I", "-m", "5", "-sS", "-o", "/dev/null", "-w", "%{http_code}", target], timeout=8.0)
            if result.returncode == 0:
                code_str = result.stdout.strip()
                if code_str and code_str != "000":
                    return True
        except Exception:
            pass
        return False
    elif ":" in target:
        # host:port 探活：nc -z -w2
        host, _, port = target.rpartition(":")
        try:
            result = await run_whitelisted("nc", ["-z", "-w2", host, port], timeout=5.0)
            return result.returncode == 0
        except Exception:
            return False
    else:
        # 纯域名/IP：ping
        try:
            result = await run_whitelisted("ping", ["-c", "1", "-W", "3", target], timeout=5.0)
            return result.returncode == 0
        except Exception:
            return False


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

async def intake(
    objective: str,
    llm: BaseLLM,
    tools: Registry,
) -> IntakeResult:
    """把自然语言 objective 解析成 IntakeResult。

    :param objective: 用户输入的自然语言任务
    :param llm: LLM 实例（用于题面抽取）
    :param tools: 工具注册表（用于 list_dir / read_file）
    :returns: IntakeResult（targets 已验活，notes 含 internal_addr）
    """
    result = IntakeResult(objective=objective)

    # a. 正则抽 URL / host:port / 域名
    candidates: list[str] = []
    candidates.extend(_extract_urls(objective))
    candidates.extend(_extract_hostports(objective))
    candidates.extend(_extract_domains(objective))

    # b. 本地路径 → list_dir → 读题面
    paths = _extract_paths(objective)
    description_parts: list[str] = [objective]
    internal_addrs_from_regex: list[str] = []

    for path_str in paths:
        if not _is_allowed_path(path_str):
            result.notes.append(f"路径不允许: {path_str}")
            continue

        p = Path(path_str)
        if p.is_dir():
            # 读题面
            docs = await _find_and_read_docs(tools, str(p))
            if docs and not docs.startswith("Error"):
                description_parts.append(f"\n[目录 {path_str} 题面文件]\n{docs}")
            else:
                description_parts.append(f"\n[目录 {path_str}] {docs or '（无题面文件）'}")
        elif p.is_file():
            read_tool = tools.get("read_file")
            if read_tool:
                content = await read_tool.run(str(p))
                if not content.startswith("Error"):
                    description_parts.append(f"\n[文件 {path_str}]\n{content[:6000]}")

    # 合并完整题面
    full_description = "\n".join(description_parts)

    # 从题面中正则抽内网地址（排除已在 URL 中的）
    # 先去掉 URL，避免把 http://10.x.x.x:port 中的 IP 误判为 internal
    text_no_urls = _URL_RE.sub("", full_description)
    text_no_urls = _HTTP_SPACE_RE.sub("", text_no_urls)
    _internal_re = re.compile(
        r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3})\b"
    )
    for m in _internal_re.findall(text_no_urls):
        addr = m if isinstance(m, str) else m[0]
        if addr not in internal_addrs_from_regex:
            internal_addrs_from_regex.append(addr)
            result.notes.append(f"internal_addr: {addr}（不作为扫描目标）")

    # c. 题面交 LLM 抽取
    extracted = await _llm_extract(llm, full_description)

    # d. 取 access 作为 targets；internal_addr 只写 notes
    access_targets: list[str] = []
    for t in extracted.get("access", []):
        if isinstance(t, str) and t.strip():
            t = t.strip()
            if t not in access_targets and t not in result.targets:
                access_targets.append(t)

    for addr in extracted.get("internal_addr", []):
        if isinstance(addr, str) and addr.strip():
            addr = addr.strip()
            note = f"internal_addr: {addr}（不作为扫描目标）"
            if note not in result.notes:
                result.notes.append(note)

    # 合并：LLM 抽取的 access 优先，正则候选补充
    for t in access_targets:
        if t not in result.targets:
            result.targets.append(t)
    for t in candidates:
        if t not in result.targets and not any(
            addr in t for addr in internal_addrs_from_regex
        ):
            result.targets.append(t)

    # 过滤内网地址：绝不作为扫描目标
    filtered_targets: list[str] = []
    for t in result.targets:
        if any(addr in t for addr in internal_addrs_from_regex):
            note = f"已过滤内网地址: {t}"
            if note not in result.notes:
                result.notes.append(note)
            continue
        filtered_targets.append(t)
    result.targets = filtered_targets

    # e. preflight 探活
    reachable: list[str] = []
    tried: list[str] = []
    for t in result.targets:
        tried.append(t)
        ok = await _preflight(t)
        if ok:
            reachable.append(t)
        else:
            result.notes.append(f"unreachable: {t}")

    result.targets = reachable

    # objective 带上题面全文
    result.objective = full_description

    logger.info(
        "intake.complete",
        targets=result.targets,
        notes_count=len(result.notes),
        objective_len=len(result.objective),
    )
    return result


__all__ = ["IntakeResult", "intake"]
