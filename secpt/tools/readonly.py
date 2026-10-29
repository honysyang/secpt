"""只读情报工具（M5-28）：http_get / read_file / list_dir。

为什么需要它们：被动侦察（crt.sh / 被动 DNS / 证书透明度）在只有 nmap 的工具面
上是死局——工单禁止主动扫描，nmap 无路可走，worker 只能反复点名不存在的
bash/shell/http_get（实测 54 张小票里 49 张是"未知工具"）。补齐三个只读入口后
"取第三方被动源 → 落 raw 证据 → declare_finding → R1 重放核验"整条链才走得通。

安全边界（为什么是三个专用工具而不是一个通用 shell）：
- 不执行任何外部命令：网络走 urllib（只认 http/https），文件读/目录列被 roots
  白名单限制在 raw/evidence 目录内——模型拿不到 .env、密钥这类账外文件；
- http_get 的产物必须落盘为 raw 文件（证据锚是真实产物，不是模型自述），
  "自撰一份报告当证据"的路被堵死：只有外部观测能造出证据文件；
- 三者都声明 read_only=True（R1 可重放核验），且不提供任何写/删原语——写面
  仍只有工具自身的产物目录。

分层纪律：本模块只署回执（exit_code/summary/raw_ref），不落票、不碰账本；
schema 校验、Guard 过闸、探针窗口仍由 registry 统一前置。
"""

from __future__ import annotations

import urllib.error
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from secpt.tools.registry import ToolManifest, ToolOutput

_USER_AGENT = "secpt/0.1 (read-only passive recon)"
# 响应体上限：证据要够核验，又不能让一次 GET 把盘写满。
_MAX_BODY = 512 * 1024
_MAX_LINES = 400
_HTTP_TIMEOUT_S = 20.0
# read_file 摘要携带的正文上限（M5-32）：小文件整文可见，大文件截断 + offset 续读。
_SUMMARY_CONTENT_CHARS = 1200


class Opener(Protocol):
    """http_get 的取数契约（注入点：测试零网络、杀进程可复现）。"""

    def __call__(self, url: str, timeout_s: float) -> tuple[int, Mapping[str, str], bytes]: ...


def _default_opener(url: str, timeout_s: float) -> tuple[int, Mapping[str, str], bytes]:
    """真实取数：urllib 只认 http/https；4xx/5xx 也照收（错误页同样是观测事实）。"""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            return int(response.status), dict(response.headers.items()), response.read(_MAX_BODY)
    except urllib.error.HTTPError as exc:  # 404 之类的响应体也是证据
        return int(exc.code), dict(exc.headers.items()), exc.read(_MAX_BODY)


class HttpGetTool:
    """只读 GET：把第三方被动源的响应落成 raw 证据文件。

    M5-31 两个感知补口（不改执行语义）：
    - 摘要带关键响应头（Server/Content-Type/Content-Length/ETag/Location）——
      版本指纹、SPA 回退判别（ETag/长度对比）全靠这几个头，此前模型只能靠
      evidence_digest 的文件头 400 字符"碰"到，逼它反复重抓；
    - 同 URL 重复抓取计数进摘要（"本 run 第 N 次"）——不给缓存（R1 重放必须是
      真复跑，缓存命中=自己跟自己比对，等于架空核验），但模型能看见"这个源
      你已经抓过 N 次、首见产物在 raw/xxx"，重复 502/超时的浪费被点名。
    """

    _SUMMARY_HEADERS = ("server", "content-type", "content-length", "etag", "location")

    manifest = ToolManifest(
        name="http_get",
        kind="mcp",
        description="只读 GET 一个 http/https URL 并把响应落成 raw 证据（被动源采集用）",
        params={"url": "str", "timeout_s": "int"},
        required=("url",),
        # GET 无副作用：R1 重放可按稳定行比对核验（时间戳/耗时在裁判侧剔除）。
        read_only=True,
        parallel_safe=True,
    )

    def __init__(self, raw_dir: Path | str = "raw", opener: Opener | None = None) -> None:
        self._raw_dir = Path(raw_dir)
        self._opener = opener or _default_opener
        self._seen: dict[str, tuple[int, str]] = {}  # url → (第几次, 首见 raw 路径)

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        """执行一次 GET：任何失败都转回执（E_TOOL_FAIL），绝不抛错打断主循环。"""
        url = (args.get("url") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return self._fail(f"E_TOOL_FAIL: http_get 只接受 http/https URL，实得 {url!r}", args)
        repeat_note = ""
        seen = self._seen.get(url)
        if seen is not None:
            repeat_note = f"（本 run 第 {seen[0] + 1} 次抓取同 URL，首见 {seen[1]}）"
        timeout_s = _parse_timeout(args.get("timeout_s"))
        try:
            status, headers, body = self._opener(url, timeout_s)
        except Exception as exc:  # noqa: BLE001  网络失败一律回执（含 DNS/超时/TLS）
            return self._fail(f"E_TOOL_FAIL: GET {url} 失败 {type(exc).__name__}: {exc}", args)
        raw_path = self._write_raw(url, status, headers, body)
        if seen is None:
            self._seen[url] = (1, str(raw_path))
        else:
            self._seen[url] = (seen[0] + 1, seen[1])
        header_note = self._header_note(headers)
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=0,
            summary=f"HTTP {status} {url}（{len(body)} bytes）{repeat_note}{header_note}"
            f"→ {raw_path}",
            raw_ref=str(raw_path),
        )

    def _header_note(self, headers: Mapping[str, str]) -> str:
        """关键响应头压进摘要（一行、有界）：指纹判别要用的头直接可见。

        展示保留响应里的原始头名大小写（"Server: nginx" 而非 "server: nginx"）
        ——摘要会被模型当证据读，头名形态与 raw 原文一致少一次心算。
        """
        lowered = {key.lower(): (key, value) for key, value in headers.items()}
        parts = [
            f"{lowered[key][0]}: {lowered[key][1][:80]}"
            for key in self._SUMMARY_HEADERS
            if lowered.get(key, ("", ""))[1].strip()
        ]
        return "；" + "；".join(parts) if parts else ""

    def _fail(self, summary: str, args: Mapping[str, str]) -> ToolOutput:
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=None,
            summary=summary,
            raw_ref="",
        )

    def _write_raw(self, url: str, status: int, headers: Mapping[str, str], body: bytes) -> Path:
        """raw 产物唯一生成点：指针必须指向刚写出的真文件（诚实锚点）。"""
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        path = self._raw_dir / f"http_{uuid.uuid4().hex}.txt"
        rendered_headers = "\n".join(f"{key}: {value}" for key, value in headers.items())
        text = body.decode("utf-8", errors="replace")
        path.write_text(
            f"$ http_get {url}\n\n--- status ---\n{status}\n"
            f"--- headers ---\n{rendered_headers}\n\n--- body ---\n{text}",
            encoding="utf-8",
        )
        return path


def _parse_timeout(raw: str | None) -> float:
    """timeout_s 解析：非法/越界一律回落默认（超时是保护，不该因笔误失效）。

    M5-32 上限 120→30：被动源查询没有合法的 40s+ 场景（实测模型给 dns.google
    传 40s，白烧一整个等待窗）；crt.sh 慢响应 18.7s 仍在限内。
    """
    try:
        value = float(raw) if raw else _HTTP_TIMEOUT_S
    except (TypeError, ValueError):
        return _HTTP_TIMEOUT_S
    if value <= 0 or value > 30:
        return _HTTP_TIMEOUT_S
    return value


class _RootedTool:
    """只读文件工具的公共边界：路径必须落在 roots 内（防 ../ 逃逸与绝对路径）。"""

    def __init__(self, roots: Sequence[Path | str] = ("raw", "evidence")) -> None:
        self._roots = tuple(Path(root) for root in roots)

    def _resolve(self, raw: str, *, want_dir: bool = False) -> Path:
        """在 roots 内解析路径：两种合法写法都收，越界/不存在一律 ValueError。

        为什么同时收"裸名"和"raw/xxx"：工具回执里的 raw_ref 是 ``raw/http_x.txt``
        （cwd 相对），而本工具的根已经是 raw——模型照抄 raw_ref 必然失败。实测
        一轮 46 张废票就是这么产生的（模型到第 49 步才自己悟出要去前缀）。两种
        写法都落在白名单根内，安全边界不变。
        """
        text = (raw or "").strip()
        if not text:
            raise ValueError("path 为空")
        given = Path(text)
        if given.is_absolute():
            raise ValueError("只接受相对路径（写 xxx.txt 或 raw/xxx.txt 均可）")
        kind = "目录" if want_dir else "文件"
        for root in self._roots:
            base = root.resolve()
            for candidate in ((base / given).resolve(), (Path.cwd() / given).resolve()):
                if not candidate.is_relative_to(base):
                    continue
                if want_dir and candidate.is_dir():
                    return candidate
                if not want_dir and candidate.is_file():
                    return candidate
        raise ValueError(
            f"只读根内找不到该{kind}：{text}"
            f"（roots={[str(r) for r in self._roots]}；可写 xxx.txt 或 raw/xxx.txt）"
        )


class ReadFileTool(_RootedTool):
    """只读读文件：只允许 raw/evidence 内的文本，限行返回，支持 offset 续读。

    M5-31 为什么必须能续读：真实 run 实测模型对同一文件连读 10+ 步——摘要只给
    前 3 行，读 60 行也只见 200 字符，模型永远拿不到 crt.sh 的 JSON 正文，只能
    原地重读。``offset``（1 起始行号）让"接着读"成为一句话的事；摘要明说
    "本读区间/剩余行数/怎么续读"，读一遍就能拿全。

    M5-32 内容进摘要：摘要是模型唯一看得到 read_file 结果的通道，实测一个
    16 行的小文件仍被同参数重读 7 次——所读区间的正文折叠进摘要（上限
    ``_SUMMARY_CONTENT_CHARS``），小文件一次读完；超上限才提示分段续读。
    """

    manifest = ToolManifest(
        name="read_file",
        kind="mcp",
        description="只读读取 raw/evidence 目录内的文本文件（限行；offset 可续读后半段）",
        params={"path": "str", "max_lines": "int", "offset": "int"},
        required=("path",),
        read_only=True,
        parallel_safe=True,
    )

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        try:
            target = self._resolve(args.get("path") or "")
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, ValueError) as exc:
            return ToolOutput(
                tool_name=self.manifest.name,
                args=dict(args),
                exit_code=None,
                summary=f"E_TOOL_FAIL: read_file {exc}",
                raw_ref="",
            )
        limit = _parse_lines(args.get("max_lines"))
        offset = _parse_offset(args.get("offset"))
        if offset > len(lines):
            offset = max(1, len(lines))
        shown = lines[offset - 1 : offset - 1 + limit]
        # M5-32 所读区间的正文折叠进摘要（小文件整文可见，模型不必重读）。
        body = " ".join(line.strip() for line in shown if line.strip())
        content = body[:_SUMMARY_CONTENT_CHARS]
        remaining = len(lines) - (offset - 1 + len(shown))
        summary = (
            f"{args.get('path')}（共 {len(lines)} 行，本读 {offset}..{offset - 1 + len(shown)})"
        )
        if remaining > 0:
            summary += f"，剩 {remaining} 行未读：offset={offset + len(shown)} 续读"
        if content:
            summary += f"：{content}"
        if len(body) > len(content):
            summary += "…（本段超出摘要上限已截断，请用 offset 分段读）"
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=0,
            summary=summary,
            # M5-30 关键：读到的文件就是证据锚。raw_ref 为空会让"读到了什么"无法
            # 进入下一步注意力包——模型只能反复重读同一文件（实测 98 张票读 26 个
            # 文件）。read_file 是只读工具，重放核验天然安全。
            raw_ref=str(target),
        )


class ListDirTool(_RootedTool):
    """只读列目录：只允许 raw/evidence 内的目录，防止把工具当探测器用。"""

    manifest = ToolManifest(
        name="list_dir",
        kind="mcp",
        description="只读列 raw/evidence 目录内的条目（名称+大小，供找证据文件用）",
        params={"path": "str"},
        required=(),
        read_only=True,
        parallel_safe=True,
    )

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        try:
            target = self._resolve(args.get("path") or ".", want_dir=True)
            # M5-32 按修改时间新→旧：raw 目录跨 run 累积，按名字排序会把当前 run
            # 的产物挤出一百项窗口——最近落盘的才是本 run 要找的证据。
            entries = sorted(target.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True)[
                :100
            ]
        except (OSError, ValueError) as exc:
            return ToolOutput(
                tool_name=self.manifest.name,
                args=dict(args),
                exit_code=None,
                summary=f"E_TOOL_FAIL: list_dir {exc}",
                raw_ref="",
            )
        names = "、".join(item.name for item in entries) or "（空目录）"
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=0,
            summary=f"{target}：{len(entries)} 项（{names}）"[:200],
            raw_ref="",
        )


def _parse_lines(raw: str | None) -> int:
    """max_lines 解析：默认 120，夹在 1.._MAX_LINES（防一次读穿大文件）。"""
    try:
        value = int(raw) if raw else 120
    except (TypeError, ValueError):
        return 120
    return max(1, min(value, _MAX_LINES))


def _parse_offset(raw: str | None) -> int:
    """offset 解析（M5-31）：默认 1（文件头），非法/越界回落 1，不因笔误失效。"""
    try:
        value = int(raw) if raw else 1
    except (TypeError, ValueError):
        return 1
    return max(1, value)


__all__ = ["HttpGetTool", "ListDirTool", "Opener", "ReadFileTool"]
