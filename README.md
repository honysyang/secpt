# secpt

无人值守的安全测试智能体 v0（骨架）。给定目标清单 → 无人值守执行 → 产出带证据链的报告。

> 仅面向**获得书面授权**的渗透测试、红队演练与靶场评测。

- 架构权威：`ARCHITECTURE.md`（分层 / 模块树 / 接口契约）
- 施工单：`BUILD_SPEC.md`（v0 逐文件实现与验收）
- 设计依据：`CONTROL_PLANE.md`

Python 3.11+，依赖仅 `pydantic` / `httpx` / `pyyaml`。CLI 命令 `secpt`，Python 包名 `pa`，环境变量前缀 `SECPT_`。

## 已知限制

- **planner-driven，无主控回合**：v0 只有引导时的一次计划 + worker 的
  有界 ReAct，CONTROL_PLANE 里的 core/decide 主控回合未实现——运行中
  不做全局重规划（`plan.revised` 通道在模型与事件层就绪，scheduler
  尚未消费）。
- **策略未数据化、未版本化**：范围与策略来自 `mission.json` 文本，
  没有策略库、没有版本管理，同一 objective 两次运行可能拆出不同计划。
- **无跨客户隔离与检索过滤**：所有 mission 共用静态 `skills/` 目录，
  事件记忆（单 mission 账本）不跨 mission 检索。
- **无评测基线**：没有回归评测集，模型/提示词改动的效果无量化对照。
- **无经验→技能写回**：运行中产出的 lessons 不回流为技能资产。
- **spent_seconds 不可复现**：预算里的秒数按墙钟结算，中断恢复后无法与
  不中断跑法逐字段一致。因此 `budget.seconds` 只当保险丝（如租约过期
  判定），**不得作为主要终止判据**。默认终止看 `budget.tokens` /
  `budget.steps`，平台期看 `no_progress_steps`。
- 平台期收口会区分原因（`meta["plateau_reason"]`）：
  `no_ready`（任务排空、验收未达成）/ `thrashing`（任务把 3 次尝试
  耗尽仍失败，无效重试）。报告与 CLI 都会显示该原因。

## 环境变量

一律 `SECPT_` 前缀，唯一读取入口在 `pa/envs.py`（业务代码不直接 getenv）：

| 变量 | 取值 | 用途 |
| --- | --- | --- |
| `SECPT_BASE_URL` | URL | 真实 LLM 端点（OpenAI 兼容） |
| `SECPT_API_KEY` | 密钥 | 真实 LLM 鉴权（只存私有属性，不出现在日志/repr/异常里） |
| `SECPT_MODEL` | 模型名 | 主模型 |
| `SECPT_FALLBACK_MODEL` | 模型名 | 降级目标（enforce 下 vs_rate 越线写入 model.switched） |
| `SECPT_LLM_MOCK` | JSON 脚本 | MockLLM 注入（测试/演示）；设置后忽略真实端点 |
| `SECPT_MENTOR_MODE` | `off`/`shadow`/`enforce` | 教练档位，默认 `shadow`（只落 advisory 不动手） |
| `SECPT_DIE_AT_STEP` | 正整数 | 测试钩子：第 N 个超级步 `os._exit(9)` 模拟断电 |

## 退出码

`0` completed（验收达成） / `2` budget（预算耗尽） / `3` plateau（平台期） /
`4` halted（熔断：deadline 或 interrupted） / `1` error（异常；`resume`
不存在的目录同样返回 1，不静默新建）。

## 术语

- **事件记忆（短期记忆）**：`events.jsonl` 事件账本——单个 mission 内
  的唯一事实源，可随时快照+重放重建。
- **长期记忆**：跨 mission 的共享资产（经验、技能库）。⚠️ v0 未实现：
  当前 `skills/` 是人工维护的静态目录，没有经验→技能的自动写回，
  也没有跨客户隔离与检索过滤。
