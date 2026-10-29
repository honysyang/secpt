# secpt — 渗透测试智能体 harness

> **一句话**：LLM 负责"聪明"（理解目标、想下一步），骨架负责"不犯低级错误"
> （状态机管流程、黑板管证据、裁判管真伪、Guard 管动作）。

当前进度与设计对照、每张卡的取舍与遗留，见
[docs/harness_design_discussion.md](docs/harness_design_discussion.md)；
需求与开发蓝本见 [docs/pentest-agent-goals-requirements-v0.1.md](docs/pentest-agent-goals-requirements-v0.1.md)
与 [docs/pentest-agent-development-doc-v0.1.md](docs/pentest-agent-development-doc-v0.1.md)。

## 快速开始

```bash
# 安装（Python 3.12+）
pip install -e ".[dev]"

# 离线演示：mock provider，零网络、零 API Key
LLM_PROVIDER=mock secpt run --goal "对内网 10.0.0.1 做端口摸底"

# 接真模型（OpenAI 兼容协议）
export LLM_PROVIDER=openai
export SECPT_LLM_BASE_URL=https://api.example.com/v1
export SECPT_LLM_API_KEY=sk-xxx
export SECPT_LLM_MODEL=your-model
secpt run --goal "对 10.0.0.5 做资产测绘并给出可利用面"
```

## 八条命令

| 命令 | 说明 |
|---|---|
| `secpt run --goal "..."` | 渗透任务模式：进场三问 → 主循环 → 出场六验 → 报告 |
| `secpt chat "..."` | 对话模式：单轮直答，不进闭环、不落账 |
| `secpt resume <task_id>` | 断电续跑：只复建不回放，从水位线 +1 继续 |
| `secpt tools` | 工具货架（manifest + 结构化参数 + tier + sha256） |
| `secpt evidence <task_id>` | 账本检查窗（小票 / 规划 / 发现 / 审计尾行） |
| `secpt panel [--port 8711]` | 只读 web 面板（账本 / 双图 / 规划 / 报告 / 对话气泡 / SSE） |
| `secpt metrics` | 两根验收杆：精度杆（verified 阳性率）、复利杆（经验净票） |
| `secpt assets` | 六库清单（prompts / skills / roles / knowledge 逐条可查） |

## 架构四条铁律

1. **FSM 管状态**：进场 → 工作 → 出场/异常 的迁移显式可测，循环不会悬死。
2. **黑板管证据**：sqlite 单文件账本（tasks/runs/plans/steps/tickets/findings/
   audit/experience/observations/graph），写入口只经 `store/*`，tickets/audit
   由触发器强制 append-only。
3. **Guard 管动作**：HIGH 级动作在执行前过闸，`hitl_mode=prompt` 走人工审批，
   off 档 fail-closed；worker 经审批代理由主线程代问。
4. **verify 管真伪**：五道裁判 R0 自证 → R4 oracle → R1 只读重放 → R2 双路径 →
   R3 反证（流量取证），永不重放写操作。

## 双引擎与主循环

- **步数线**：主 LLM 循环预算（`[budget].max_steps`，默认 200），撞限走异常出口。
- **事件线**：双图变化每 N 步触发一次 LLMC 规划重估（`[plan]`），失速（连续失败 /
  连续同一规划）拉铃走救场，名额有限，只提案不裁决。
- **一步**：打包注意力包（目标 / 规划 / 双图 / 小票 / 经验 / 知识 / 可派角色）→
  LLM 结构化输出 → 批量派单（并行安全工具并发）→ 同一事务落 steps/小票/规划/图/水位线。
- **出场六验**：账本交齐 + 双图一致 / findings 全终态 / 三问齐全 / 交付对目标
  （`[[acceptance]]`）/ 规划全收口 / 步骤全 ok；不通过弹回继续走，连败 3 轮异常收尾。
- **异常出口**：LLMA 出半场报告 + LLMB 沉淀失败经验（`environment=lab`）。

## 六库（分三型）

| 类型 | 库 | 位置 | 说明 |
|---|---|---|---|
| 文件资产（git 版本化） | 角色库 | `secpt/assets/roles/*.yaml` | who+how 打包，引用 skills/prompts，按 persona 挂给 worker |
| | skills 库 | `secpt/assets/skills/*.md` | 可复用动作模块，被角色/工单引用 |
| | prompts 库 | `secpt/assets/prompts/*.md` | 纯模板碎片，`{field}` 渲染 |
| | 知识库 | `secpt/assets/knowledge/*.md` | 只读方法论，按关键词命中进注意力包 |
| 数据库（migration 版本化） | 经验库 | sqlite `experience` 表 | 唯一跨 run 记忆体，只有 LLMB 收尾能写；回放只收 verified |
| 进程型（manifest+hash） | 工具库 | MCP + 本地适配器 | nmap / agent_run / declare_finding / MCP servers |

资产查找链：`SECPT_ASSETS_DIR` 覆盖目录 → 包内出厂资产；缺件抛 `E_ASSET` 不静默降级。

## 配置（`configs/profile.toml`）

| 段 | 键 | 说明 |
|---|---|---|
| `[judge]` | `enabled` | 裁判开关；`[[ground_truth]]` 为 R4 oracle 清单 |
| `[[acceptance]]` | `id` / `pattern` | 出场③"交付对目标"的机器判据（verified finding 命中） |
| `[plan]` | `review_every_steps` / `review_limit` | 规划重估节拍与每 run 名额 |
| `[budget]` | `max_steps` / `workers` | 步数线 / worker 线程数 |
| `[stall]` | `failures` / `repeat_plans` | 工具连败 / 重复规划拉铃阈值 |
| `[guard]` | `tiers` / `hitl_mode` | 可选：分级审批；缺段 = 闸常开，写错 tier 值 `E_PROFILE` |
| `[execute]` | `isolation` / `step_tool_limit` / `docker_flags` | 可选：执行隔离（默认本机 shell）与每步工具上限（默认 4） |
| `[probe]` / `[mcp]` | 见对应模块 docstring | 可选：流量探针（默认关）、MCP 服务器清单（默认无） |

关键环境变量：`SECPT_DB`、`SECPT_RAW_DIR`、`SECPT_NMAP_BIN`、`SECPT_PROFILE`、
`SECPT_ENV`（经验环境键，默认 lab）、`SECPT_WORKERS`（临时覆盖配置）、
`SECPT_ASSETS_DIR`、`SECPT_PROBE_BACKEND`、`LLM_PROVIDER`。

### 用 `.env` 配置（可选）

仓库根有模板 `env.example`，复制为 `.env`（已 gitignore）后填值。两种生效方式：

```bash
# A. 让当前 shell 生效
set -a; . ./.env; set +a

# B. 让 secpt 自己装载（只认显式路径；真实环境变量永远优先）
SECPT_ENV_FILE=.env secpt run --goal "..."
```

装载规则：只接受 `SECPT_*` 与 `LLM_PROVIDER` 两类键（写 `PATH` 之类不会被采纳），
不做变量展开与 shell 求值；`SECPT_ENV_FILE` 指空文件会 `E_CONFIG` 拒跑。

## 目录结构

```
secpt/
├── core/        # 主循环、FSM、预算、失速计、规划重估节拍、断点续跑、任务板
├── llms/        # LLM 协议层、main_llm、worker、coach(LLMC)、FakeLLM、providers/
├── judge/       # 裁判流水线 R0/R1/R2/R3/R4 + profile
├── finalize/    # 出场六验、LLMA 报告、LLMB 经验蒸馏
├── store/       # sqlite 账本、迁移、双图、经验、观测流、规划、注意力包、度量
├── tools/       # 注册表/Guard/审批代理/执行隔离/探针/MCP/适配器
├── ob/          # CLI 气泡视图、web 只读面板、事件总线
├── assets/      # 六库中的文件资产：prompts / skills / roles / knowledge
├── main.py      # CLI 八命令与装配
└── errors.py    # 分层错误码（E_CONFIG/E_PROFILE/E_LEDGER/E_STALL/...）
configs/profile.toml    # 关键参数与裁判弹药
docs/                   # 需求、开发蓝本、施工记档
tests/unit/             # 517 例离线单测（FakeLLM，零网络）
```

## 质量与测试

```bash
ruff format --check . && ruff check .   # line-length 100 / py312
pytest -q                               # 517 passed，全部离线
```

测试纪律：任何强依赖真实 LLM / 网络 / 靶场的路径都不进单测；真镜像无网、真模型
联网属手工冒烟。每张卡的取舍、可证伪证据与遗留都记在
[docs/harness_design_discussion.md](docs/harness_design_discussion.md)。

## 历史说明

本仓库早期是一个通用 agent harness（`harness/` + `runtime/` 两包，含
single/team/swarm 三种模式）。该实现已从磁盘移除，代码保留在 git 历史
（commit `0d0ffd0` 及之前），仅作查阅参照；当前代码与本文档只描述渗透测试骨架。
