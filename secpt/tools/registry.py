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
from functools import partial
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from secpt.errors import McpError
from secpt.tools.guard import E_GUARD_DENY, Approver, GuardConfig
from secpt.tools.probe import CaptureBackend, CaptureEvent, wrap_execution


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
    # read_only（M3-4 拍板①）：工具自身的诚实声明——R1 只读重放门据此放行。
    # 默认 False（未知即写操作），NmapTool 显式 True；写操作永不 rerun 是机制。
    read_only: bool = False
    # parallel_safe（M5-12）：本工具能否与同批其它 parallel_safe 工具并发执行。
    # 默认 False（未知即不可并发）——只对"无共享可变状态"的工具开（nmap 子进程/
    # agent_run 任务板）；写账本类（declare_finding）永远串行。
    parallel_safe: bool = False
    # free_text（M5-29）：允许含换行/制表等控制字符的参数名清单。
    # 为什么需要：write_file 的 content 天生多行，若按"控制字符注入"一刀切拒绝，
    # 智能体就写不出脚本；执行类参数（run_command 的 program/args）永远不放行。
    free_text: tuple[str, ...] = ()
    hash: str | None = None


class ToolOutput(BaseModel):
    """单次工具调用的回执：小票三字段由本层署名（raw_ref 指向 raw 产物）。

    ``denied``（M3-1 附加字段，默认 False）：Guard 拒绝标记——True 即"工具没有
    执行"，主循环据此落 audit（registry 不带 task_id，账归 loop 收口）。
    ``hitl_approved``/``hitl_note``（M3-2 附加字段）：HIGH 级过审批的准旗与其
    来源（auto=非交互自动通过 / 人工=真点过人），audit 文案永远说真话。
    ``probe_events``（M3-3 附加字段）：本次执行观测窗交割的客观事件——空元组
    = 探针没开或没抓到；落账转基元参数是 loop 的活，本层只负责把事实挂在
    回执上。
    ``made_progress``（M5-27 附加字段）：exit_code==0 只代表"没炸"，不代表
    "有进展"——agent_run 预算用尽未收工时显式声明 False，失速计据此收数，
    避免空转被当成一次成功。默认 True 保持既有工具语义不变。
    """

    tool_name: str
    args: dict[str, str]
    exit_code: int | None
    summary: str
    raw_ref: str
    denied: bool = False
    hitl_approved: bool = False
    hitl_note: str = ""
    probe_events: tuple[CaptureEvent, ...] = ()
    made_progress: bool = True
    # duration_ms（M5-29 附加字段）：本次执行耗时，纯观测元数据——由主循环/worker
    # 在 dispatch 外层计时填入，工具自己不必关心；只进事件与日志，不落库。
    duration_ms: int = 0


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


def tool_signature(manifest: ToolManifest) -> str:
    """工具签名的一行式渲染：``name(必填*, 可选:类型)``。

    为什么签名必须由 manifest 现场渲染：模型猜错参数名是实测最大失败源
    （一轮 17 张小票里 12 张 E_SCHEMA_TOOL）。签名若另写一份提示词副本，
    迟早与 manifest 漂移；同源渲染则"改登记卡即改包"，不存在两套真相。
    """
    parts = [
        f"{key}{'*' if key in manifest.required else ''}:{kind}"
        for key, kind in manifest.params.items()
    ]
    return f"{manifest.name}({', '.join(parts)})"


def manifest_hash(manifest: ToolManifest) -> str:
    """manifest 内容的确定性 sha256（不含 hash 字段本身）。"""
    payload = manifest.model_dump(exclude={"hash"}, exclude_none=True)
    canonical = repr(sorted(payload.items())).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class ToolRegistry:
    """唯一登记点：tools 层只此一个 dispatch，主循环经 Dispatcher 协议转进来。"""

    def __init__(
        self,
        adapters: Sequence[ToolAdapter],
        *,
        guard: GuardConfig | None = None,
        approver: Approver | None = None,
        probe: CaptureBackend | None = None,
    ) -> None:
        self._adapters: dict[str, ToolAdapter] = {}
        self._guard = guard  # None = 闸常开（拍板③ no-op），上班后拦截点在执行器调用前
        self._approver = approver  # M3-2：HIGH 级审批人；Guard 上班但缺人 = fail-closed
        self._probe = probe  # M3-3：探针后端；None = 不开窗（R6 关档零开销）
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

    def tool_catalog(self) -> list[str]:
        """注意力包用的一行式工具目录（M5-27）：签名 + 用途 + 本 run 参数失败数。

        为什么要进包：模型此前只能从一句描述里猜参数名——实测 12/17 张失败小票
        全是猜错参数/缺必填。签名是"可执行信息"，必须每步都在感知面上；渲染与
        manifest 同源（不是提示词副本），改登记卡即改包。失败计数让"反复试错"
        从隐性变成显性：同一工具被拒多次时，行内直接点名，模型不必再猜第二轮。
        """
        lines: list[str] = []
        for manifest in self.manifests():
            line = tool_signature(manifest)
            if manifest.description:
                line += f" — {manifest.description}"
            with self._lock:
                failures = self.schema_failures.get(manifest.name, 0)
            if failures:
                line += f"（本 run 已因参数不合失败 {failures} 次，务必按上述参数名）"
            lines.append(line)
        return lines

    def tier_of(self, name: str) -> str:
        """货架分级查询（CLI 展示用）：guard 不上班时空串（M1/M2 老口径）。"""
        return self._guard.tier_of(name) if self._guard is not None else ""

    def get(self, name: str) -> ToolAdapter | None:
        return self._adapters.get(name)

    def parallel_safe(self, name: str) -> bool:
        """本工具能否与同批其它工具并发（M5-12）：未知工具一律 False。"""
        tool = self._adapters.get(name)
        return bool(tool is not None and tool.manifest.parallel_safe)

    def dispatch(self, name: str, args: Mapping[str, str]) -> ToolOutput:
        """§6.2 契约入口：schema 校验→dispatch→（落票归主循环）。业务失败不抛不重试。"""
        return self._dispatch(name, args)

    def readonly_replay(self, name: str, args: Mapping[str, str]) -> ToolOutput | None:
        """R1 只读重放门（M3-4 拍板①）：写操作/未知工具返回 None 且**零执行**。

        为什么拦在这里而不是 judge：manifest 的 read_only 是工具自身的诚实声明，
        注册表是唯一掌握 manifest 的层——"永不 rerun 写操作"（蓝本 L371）在此
        机制化，不靠裁判自觉。只读工具照走 _dispatch 全管线：schema 校验、Guard/HITL 同权过闸
        （§3.6 Guard 同权——重放不是特权通道）、探针窗口照开。
        """
        tool = self._adapters.get(name)
        if tool is None or not tool.manifest.read_only:
            return None
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
            # 回执自带合法签名（M5-27）：模型一次猜错后不必再靠试错逼近参数名，
            # 修正信息与失败原因同一条小票送达（感知面闭环，不额外消耗一步）。
            return reject(
                f"E_SCHEMA_TOOL: {problem}；合法签名 {tool_signature(tool.manifest)}", name, args
            )
        # M3-1 拦截点（§2.1 L102"调用前"）：schema 合格的调用才劳 Guard 动闸——
        # 白名单在外、分级在内两级语义；拒绝不发执行器，回执照落小票（audit 归 loop）。
        # M3-2：HIGH 级带 HITL 准旗回家；无审批人=缺通道 fail-closed；拒=走 denied 通道。
        hitl_approved = False
        hitl_note = ""
        if self._guard is not None:
            allowed, verdict = self._guard.check(name)
            if not allowed:
                return reject(verdict, name, args, denied=True)
            if self._guard.tier_of(name) == "high":
                if self._approver is None:
                    return reject(
                        f"{E_GUARD_DENY}: {name} 为 HIGH 级动作且无审批通道，fail-closed",
                        name,
                        args,
                        denied=True,
                    )
                if not self._approver.decide(name):
                    return reject(
                        f"{E_GUARD_DENY}: 人工拒绝执行 {name}（HITL 弹窗拒路）",
                        name,
                        args,
                        denied=True,
                    )
                hitl_approved = True
                # note 展示层用中文可读文案；kind 只区分 auto/人工两族。
                hitl_note = "auto" if self._approver.kind == "auto" else "人工"
        # M3-3 执行窗（R6 关档零开销）：probe=None 直接走原路径，不碰 start/stop；
        # 开档则窗只包执行器调用本身——guard 拒绝/递归拒绝在上面已 return，零抓取。
        probe_events: tuple[CaptureEvent, ...] = ()
        if tool.manifest.kind == "in-process":
            if _in_process_depth() > 0:
                # §3.6 规则 2：栈上已有 in-process 帧又点名派单 = 套娃，拒绝
                return reject("E_SCHEMA_TOOL: in-process 工具禁递归", name, args)
            if self._probe is None:
                output = _execute_in_process(self, tool, args)
            else:
                output, probe_events = wrap_execution(
                    self._probe, partial(_execute_in_process, self, tool, args)
                )
        elif self._probe is None:
            output = tool.run(args)
        else:
            output, probe_events = wrap_execution(self._probe, partial(tool.run, args))
        # 尾部统一签署：执行器自身不知道审批/探针，回执由本层署真（§3.2 分层）。
        updates: dict[str, object] = {}
        if hitl_approved:
            updates["hitl_approved"] = True
            updates["hitl_note"] = hitl_note
        if probe_events:
            updates["probe_events"] = probe_events
        return output.model_copy(update=updates) if updates else output

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
            if key in manifest.free_text:
                continue  # 文本正文：多行是常态（仍然只落盘，不参与任何解释执行）
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
    "tool_signature",
]
