"""learning：经验闭环（系统越用越好用）。

每次运行结束 → LLM 从 transcript 蒸馏经验 → var/knowledge/experiences.jsonl
（append-only，sha1 去重）；下次任务启动时按 objective 关键词检索 top-3 注入
system prompt 的【历史经验】段。

设计红线：
- 学习是 best-effort：蒸馏/检索任何失败都静默跳过，绝不影响主链路
- 经验只来自真实运行沉淀，不做人工编造（可 `secpt learn --job <id>` 手动触发）
- 通用机制：不针对任何具体靶场
"""

from secpt.learning.distill import distill_job
from secpt.learning.retrieve import experience_section

__all__ = ["distill_job", "experience_section"]
