# secpt

无人值守的安全测试智能体 v0（骨架）。给定目标清单 → 无人值守执行 → 产出带证据链的报告。

> 仅面向**获得书面授权**的渗透测试、红队演练与靶场评测。

- 架构权威：`ARCHITECTURE.md`（分层 / 模块树 / 接口契约）
- 施工单：`BUILD_SPEC.md`（v0 逐文件实现与验收）
- 设计依据：`CONTROL_PLANE.md`

Python 3.11+，依赖仅 `pydantic` / `httpx` / `pyyaml`。CLI 命令 `secpt`，Python 包名 `pa`，环境变量前缀 `SECPT_`。

## 已知限制

- **spent_seconds 不可复现**：预算里的秒数按墙钟结算，中断恢复后无法与
  不中断跑法逐字段一致。因此 `budget.seconds` 只当保险丝（如租约过期
  判定），**不得作为主要终止判据**。默认终止看 `budget.tokens` /
  `budget.steps`，平台期看 `no_progress_steps`。
- 平台期收口会区分原因（`meta["plateau_reason"]`）：
  `no_ready`（任务排空、验收未达成）/ `thrashing`（任务把 3 次尝试
  耗尽仍失败，无效重试）。报告与 CLI 都会显示该原因。
