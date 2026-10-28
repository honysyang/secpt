"""工具注册表：manifest 声明 + dispatch（§3.5/§3.6/WF3，§2.3 唯一登记点）。

职责边界（为什么 registry 不落票、不重试）：
- WF3 把序钉死为「manifest schema 校验 → dispatch → 小票追加」：小票是 store/ledger
  的唯一权限，本层只署回执（exit_code/summary/raw_ref），由主循环落票——dispatch
  不反串账本写手。
- 业务失败（schema 不合 / exit_code≠0 / 超时）一律转成回执落小票（E_SCHEMA_TOOL /
  E_TOOL_FAIL 语义见 §9），绝不上抛、绝不重试；只有注册冲突这类编程错误才抛
  McpError（注册发生在装配期，不在 step 循环里）。
- 禁递归按调用栈拒绝（§3.6 对表规则 2）：in-process 工具执行期间，dispatch 再被
  调到第二个 in-process 工具时栈里已存在执行帧，直接拒绝落回执——机制在栈，
  不靠习惯自觉。
"""

from __future__ import annotations

import hashlib
import inspect
import threading
from collections.abc import Mapping, Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from secpt.errors import McpError
from secpt.tools.guard import GuardConfig


class ToolManifest(BaseModel):
    """工具的自描述登记卡（§3.5：进程清单 manifest+hash）。

    ``hash`` 在注册时从 manifest 内容确定性生成（sha256），同一份声明永远
    同一个哈希——CLI ``secpt tools`` 展示用，也是 M3 Guard 的比对锚。
    """

    name: str
    kind: Literal["mcp", "in-process"] = "mcp"
    description: str = ""
    params: dict[str, Literal["str", "int", "float"]] = Field(default_factory=dict)
    required: tuple[str, ...] = ()
    hash: str | None = None


class ToolOutput(BaseModel):
    """单次工具调用的回执：小票三字段由本层署名（raw_ref 指向 raw 产物）。

    ``denied``（M3-1 附加字段，默认 False）：Guard 拒绝标记——True 即"工具没有
    执行"，主循环据此落 audit（registry 不带 task_id，账归 loop 收口）。
    """

    tool_name: str
    args: dict[str, str]
    exit_code: int | None
    summary: str
    raw_ref: str
    denied: bool = False


class ToolAdapter(Protocol):
    """适配器契约：登记一份 manifest，吃参、干工、吐回执（不落票不重试）。"""

    manifest: ToolManifest

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        """执行一次调用。业务失败也回 ToolOutput，禁止抛错打断主循环。"""
        ...


_GUARD_FRAME = "_execute_in_process"


def _execute_in_process(
    registry: ToolRegistry, tool: ToolAdapter, args: Mapping[str, str]
) -> ToolOutput:
    """in-process 工具的执行帧：栈里能数到它 = 此刻有 in-process 工具在跑。"""
    return tool.run(args)


def _in_process_depth() -> int:
    """当前调用栈上 in-process 执行帧的数量（本函数自身不算）。"""
    return sum(
        1
        for frame in inspect.stack()
        if frame.function == _GUARD_FRAME and frame.filename.endswith("registry.py")
    )


def reject(
    summary: str, tool_name: str, args: Mapping[str, str], *, denied: bool = False
) -> ToolOutput:
    """构造拒绝回执的小工厂（schema 不过=拒绝落票，不是抛错；M3-1 起可带 denied 标记）。"""
    return ToolOutput(
        tool_name=tool_name,
        args=dict(args),
        exit_code=None,
        summary=summary,
        raw_ref="",
        denied=denied,
    )


def manifest_hash(manifest: ToolManifest) -> str:
    """manifest 内容的确定性 sha256（不含 hash 字段本身）。"""
    payload = manifest.model_dump(exclude={"hash"}, exclude_none=True)
    canonical = repr(sorted(payload.items())).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class ToolRegistry:
    """唯一登记点：tools 层只此一个 dispatch，主循环经 Dispatcher 协议转进来。"""

    def __init__(
        self, adapters: Sequence[ToolAdapter], *, guard: GuardConfig | None = None
    ) -> None:
        self._adapters: dict[str, ToolAdapter] = {}
        self._guard = guard  # None = 闸常开（拍板③ no-op），上班后拦截点在执行器调用前
        self._lock = threading.Lock()
        self.schema_failures: dict[str, int] = {}
        for adapter in adapters:
            manifest = adapter.manifest
            manifest.hash = manifest.hash or manifest_hash(manifest)
            if manifest.name in self._adapters:
                raise McpError(f"工具重复注册: {manifest.name}", context={"name": manifest.name})
            self._adapters[manifest.name] = adapter

    def manifests(self) -> list[ToolManifest]:
        """``secpt tools`` 的货架清单（manifest+hash 一并露出）。"""
        return [adapter.manifest for adapter in self._adapters.values()]

    def tier_of(self, name: str) -> str:
        """货架分级查询（CLI 展示用）：guard 不上班时空串（M1/M2 老口径）。"""
        return self._guard.tier_of(name) if self._guard is not None else ""

    def get(self, name: str) -> ToolAdapter | None:
        return self._adapters.get(name)

    def dispatch(self, name: str, args: Mapping[str, str]) -> ToolOutput:
        """§6.2 契约入口：schema 校验→dispatch→（落票归主循环）。业务失败不抛不重试。"""
        return self._dispatch(name, args)

    def _dispatch(self, name: str, args: Mapping[str, str]) -> ToolOutput:
        tool = self._adapters.get(name)
        if tool is None:
            return reject(
                f"E_SCHEMA_TOOL: 未知工具 {name}; 已登记: {sorted(self._adapters)}", name, args
            )
        problem = self._validate(tool.manifest, args)
        if problem is not None:
            with self._lock:
                self.schema_failures[name] = self.schema_failures.get(name, 0) + 1
            return reject(f"E_SCHEMA_TOOL: {problem}", name, args)
        # M3-1 拦截点（§2.1 L102"调用前"）：schema 合格的调用才劳 Guard 动闸——
        # 白名单在外、分级在内两级语义；拒绝不发执行器，回执照落小票（audit 归 loop）。
        if self._guard is not None:
            allowed, verdict = self._guard.check(name)
            if not allowed:
                return reject(verdict, name, args, denied=True)
        if tool.manifest.kind == "in-process":
            if _in_process_depth() > 0:
                # §3.6 规则 2：栈上已有 in-process 帧又点名派单 = 套娃，拒绝
                return reject("E_SCHEMA_TOOL: in-process 工具禁递归", name, args)
            return _execute_in_process(self, tool, args)
        return tool.run(args)

    def _validate(self, manifest: ToolManifest, args: Mapping[str, str]) -> str | None:
        """manifest 输入 schema 校验：未知/缺失/类型，以及控制字符注入（§7 安全表）。"""
        values = dict(args)
        required_missing = [k for k in manifest.required if k not in values]
        if required_missing:
            return f"缺必填参数 {required_missing}"
        for key, value in values.items():
            if key not in manifest.params:
                return f"未知参数 {key}={value!r}"
            kind = manifest.params[key]
            if kind != "str":
                try:
                    int(value) if kind == "int" else float(value)
                except (TypeError, ValueError):
                    return f"参数 {key} 需 {kind}, 实得 {value!r}"
            if any(control in value for control in CONTROL_CHARS):
                return f"参数 {key} 含控制字符"
        return None


CONTROL_CHARS = frozenset(char for code in range(0x20) for char in [chr(code)]) | frozenset("\x7f")


__all__ = [
    "CONTROL_CHARS",
    "ToolAdapter",
    "ToolManifest",
    "ToolOutput",
    "ToolRegistry",
    "manifest_hash",
]
