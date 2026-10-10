"""Jev 门控：TypeSafe System One 类型化决策（评判第 ② 层）。

定位：高吞吐、低延迟、低方差的第一道判定门。
- typed decision（yes/no + 校准概率），中位延迟 ~0.15s、成本约为 LLM judge 的 ~0.4%，
  重复判定方差比 autoregressive LLM judge 低 2 个数量级（LangChain/Future AGI 2026-09 实验）
- 置信度 >= min_confidence 直接采信；低置信/不可用 → 返回 None，由调用方升级到 LLM judge

配置（环境变量）：
- JEV_API_KEY：TypeSafe API key；未设置时 available()=False，评判层自动降级为纯 LLM judge
- JEV_BASE_URL：端点（默认 https://api.typesafe.ai）
- JEV_MIN_CONFIDENCE：采信阈值（默认 0.85）

注意：TypeSafe 的 REST 形状以官方文档为准；本客户端把路径与 payload 字段集中在本文件，
接入真实 key 时如字段名有出入只需改这里。已知边界：推导密集型问题（利用链合理性分析等）
明显弱于 LLM judge，因此永远保留升级路径，不做唯一裁决。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MIN_CONFIDENCE = 0.85
TIMEOUT_S = 15.0


@dataclass
class JevDecision:
    answer: bool          # yes/no
    probability: float    # 校准概率 [0,1]
    raw_model: str = ""


class JevGate:
    """typed decision 门控；任何故障都降级为 None（不阻断评判链）。"""

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 min_confidence: float | None = None) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("JEV_API_KEY", "")
        self._base_url = (base_url or os.environ.get("JEV_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self._min_confidence = (min_confidence if min_confidence is not None
                                else float(os.environ.get("JEV_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE)))

    @property
    def min_confidence(self) -> float:
        return self._min_confidence

    def available(self) -> bool:
        return bool(self._api_key)

    async def decide(self, question: str, context: str) -> JevDecision | None:
        """返回 None 表示不可用/低置信/出错（调用方升级到 LLM judge）。"""
        if not self.available():
            return None
        import httpx
        payload = {
            "question": question,
            "type": "choice",
            "choices": ["yes", "no"],
            "context": context[:6000],
        }
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                r = await client.post(
                    f"{self._base_url}/v1/decide",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}",
                             "Content-Type": "application/json"},
                )
                r.raise_for_status()
                data = r.json()
            ans = str(data.get("answer", "")).lower()
            if ans not in ("yes", "no"):
                return None
            prob = float(data.get("probability", 0.0) or 0.0)
            return JevDecision(answer=(ans == "yes"), probability=prob,
                               raw_model=str(data.get("model", "")))
        except Exception as e:  # noqa: BLE001 - 门控故障不阻断评判
            print(f"[jev] decision failed, 降级 LLM judge: {e}")
            return None

    def accept(self, decision: JevDecision | None) -> bool:
        """高置信才采信；低置信返回 False（调用方升级 LLM judge）。"""
        return decision is not None and decision.probability >= self._min_confidence
