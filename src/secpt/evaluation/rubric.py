"""Rubric：任务完成条件的结构化表达。

rubric 是评判层的标尺：
- objective_summary：任务目标一句话摘要
- completion_conditions：完成条件列表（"拿到 flag" / "获得 root shell" / "读取到目标文件内容"）
- ground_truth：出题方提供的答案或完成态（可选；CTF 场景即 flag 原文）

来源两路：
1. 显式提供（--rubric 文件，YAML/JSON）——挑战包规范 v2 的入口
2. LLM 从题面自动生成（build_rubric）——通用任务零配置兜底
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import BaseModel


class Rubric(BaseModel):
    """评判标尺。"""

    objective_summary: str
    completion_conditions: list[str]
    ground_truth: str | None = None

    def to_prompt(self) -> str:
        gt = f"\n出题方标准答案（ground truth）：{self.ground_truth}" if self.ground_truth else ""
        conds = "\n".join(f"  - {c}" for c in self.completion_conditions)
        return (
            f"任务目标：{self.objective_summary}\n"
            f"完成条件：\n{conds}\n"
            f"{gt}\n"
            "判定标准：只有当轨迹证据明确满足至少一条完成条件时才可判 complete=true；"
            "agent 自报的结论不单独作为证据。"
        )


def load_rubric_file(path: str | Path) -> Rubric:
    """从 YAML/JSON 文件加载 rubric。"""
    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    return Rubric.model_validate(data)


_BUILD_PROMPT = (
    "你是渗透任务的评判标尺制定者。根据下面的任务描述，提取：\n"
    "1. objective_summary：任务目标一句话\n"
    "2. completion_conditions：完成条件列表（3-5 条，可判定、可验证，"
    "不要规定具体 flag 格式）\n\n"
    "只输出 JSON：{{\"objective_summary\": \"...\", \"completion_conditions\": [\"...\"]}}\n\n"
    "任务描述：\n{objective}"
)


async def _acomplete(llm, messages: list[dict], model: str) -> str:
    """统一 LLM 调用（兼容 FakeLLM.call 与 call_llm 函数签名）。"""
    if hasattr(llm, "call"):
        resp = await llm.call(messages, [], model)
    else:
        resp = await llm(messages, [], model, "")
    return (resp.choices[0].message.content or "").strip()


def _extract_json(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


async def build_rubric(objective: str, llm=None, model: str = "deepseek-chat",
                       ground_truth: str | None = None) -> Rubric:
    """由题面生成 rubric；LLM 不可用/解析失败时退化为以题面原文为唯一完成条件。"""
    if llm is not None:
        try:
            text = await _acomplete(llm, [{"role": "user", "content":
                                           _BUILD_PROMPT.format(objective=objective[:4000])}],
                                    model)
            data = _extract_json(text)
            if data and data.get("completion_conditions"):
                return Rubric(
                    objective_summary=str(data.get("objective_summary") or objective[:200]),
                    completion_conditions=[str(c) for c in data["completion_conditions"]],
                    ground_truth=ground_truth,
                )
        except Exception as e:  # noqa: BLE001 - 退化路径，不阻断评判
            print(f"[rubric] LLM 生成失败，退化使用题面原文: {e}")
    return Rubric(
        objective_summary=objective[:200],
        completion_conditions=[objective.strip()[:500]],
        ground_truth=ground_truth,
    )
