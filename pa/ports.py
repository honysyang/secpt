"""跨层协议（ARCHITECTURE §2.3 / §3）。

只放 Protocol，不放实现（坑 A2：LLMClient 实现放 infra/llm.py，runtime 只
import 本模块）。依赖规则 1：models / ports / errors 不依赖任何内部模块——
内部类型仅出现在 TYPE_CHECKING 分支，运行时本包不 import 任何 pa 内部模块。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from pa.models import Event, MissionState


class LLMClient(Protocol):
    """模型客户端（BUILD_SPEC §11.1 接口）。

    budget_tokens 是 advisory 建议值：客户端可忽略，真正的裁剪在
    core/context.py（裁定 1）。MockLLM 不要求实现该行为。
    """

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: Any,
        phase: str,
        budget_tokens: int | None = None,
    ) -> str: ...

    def parse(self, raw: str, schema: Any) -> Any: ...

    def estimate_tokens(self, text: str) -> int:
        """估算文本 token 数（纯函数：幂等，对 len(text) 单调不减）。"""
        ...


class StoreView(Protocol):
    """只读投影：所有人可用；core 在类型上就拿不到写方法（§2.3）。

    evidence_meta / evidence_verified 依据裁定 3：证据元数据走视图（LRU
    缓存），不进 MissionState。
    """

    def mission(self) -> MissionState: ...

    def events(self, from_seq: int = 1) -> Iterator[Event]: ...

    def evidence(self, eid: str) -> dict: ...

    def evidence_meta(self, eid: str) -> dict: ...

    def evidence_verified(self, eid: str) -> bool: ...


class StoreWriter(StoreView, Protocol):
    """写接口：只有 runtime 持有（§2.3 单写者）。"""

    def append(self, type: str, actor: str, payload: dict, idem_key: str) -> Event: ...

    def checkpoint(self, state: MissionState) -> None: ...
