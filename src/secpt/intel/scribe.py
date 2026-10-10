"""GraphScribe：程序自动写图（规则能写的记忆不要让 LLM 写）。

挂在 hooks 链上：engine 的 finding 信号（凭据/URL，正则已识别）自动落入 intel——
  asset(URL)   → observe_asset(endpoint)   （计覆盖度）
  credential   → fact 节点（"凭据发现： ..."）
零 LLM 成本，符合图记忆派"程序算关系、LLM 只交观察"的分工。
"""

from __future__ import annotations

import re

from secpt.intel.store import IntelStore


class GraphScribe:
    """hooks 包装：on_finding 落图后原样委托。"""

    def __init__(self, inner, *, job_id: str, store: IntelStore | None = None) -> None:
        self._inner = inner
        self._job_id = job_id
        self._st = store or IntelStore()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def on_finding(self, kind: str, value: str, evidence: str = "") -> None:
        try:
            if kind == "asset" and re.match(r"https?://", value or ""):
                self._st.observe_asset("endpoint", value, job_id=self._job_id)
            elif kind == "credential":
                self._st.add_node("fact", f"凭据发现： {value[:160]}", job_id=self._job_id)
        except Exception:
            pass
        self._inner.on_finding(kind, value, evidence)
