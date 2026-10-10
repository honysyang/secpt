"""S5 编排策略声明（policy）——明确"不做的事"。

新编排模型是**并行专业链**，不是树搜索。以下概念在本阶段正式退役，
任何新代码不得重新引入：

- 不做 LLM 评分（Evaluator / EvalScore / evidence scoring）
- 不做树搜索（Scheduler 波前 / Node / Genome / 分形树）
- 不做 DeathCertificate 降级（death / degradation / exhaustion）

现有 core/evaluator/ 与 core/node/ 已在此阶段正式删除。
编排只做三件事：
    recon  → 并行 exploit ×N → verify
每一步都是一个完整不可断裂的 Engine 链（单链语义 S0 红线原样保留）。
"""

from __future__ import annotations

# 禁止重新引入的模块名（硬约束，供审查/测试引用）
DEPRECATED_MODULES = (
    "secpt.core.evaluator",
    "secpt.core.node",
    "secpt.core.scheduler",
)

# 编排允许的唯一结构（常量文档化）
DAG_STAGES = ("recon", "exploit", "verify")

# 单入口阈值：入口数 <= 1 时编排层休眠，直连单链
SINGLE_ENTRY_MAX = 1
