# 文档索引

| 文档 | 读者 | 内容 |
|---|---|---|
| [架构现状.md](架构现状.md) | **所有人（权威）** | 当前真实架构、数据流、关键语义（bash 安全边界/状态机/评判链/上下文治理）、目录落盘约定 |
| [secpt-总设计文档（多任务并行·长任务·多场景）.md](secpt-总设计文档（多任务并行·长任务·多场景）.md) | 架构演进 | S1–S3 阶段设计（作业层/多任务/上下文四件套） |
| [secpt-开发提示词S0-S7（可直接复制）.md](secpt-开发提示词S0-S7（可直接复制）.md) | 开发历史 | S0–S7 各迭代的原始开发指令（考古用） |
| [DAG-vs-single-AB实验报告.md](DAG-vs-single-AB实验报告.md) | 决策依据 | 单链 vs DAG 编排对照实验（DAG 默认关闭的出处） |
| [archive-开发文档-v0.3.md](archive-开发文档-v0.3.md) | ❌ 勿按其实现找代码 | Phase 2 分形 Node 树/波前调度/Evaluator——**已在 S5 全部删除**（见 `src/secpt/core/orchestrator/policy.py`），仅留档 |

## 阅读路径

- 新成员/新智能体：根目录 `AGENTS.md` → `架构现状.md` → `src/secpt/runtime/engine.py`
- 想改引擎：`架构现状.md` 的"关键语义"章节（四条红线）→ `tests/unit/runtime/test_engine_smoke.py`
- 想做评测/打榜：`架构现状.md` 评判层章节 → `src/secpt/evaluation/`
