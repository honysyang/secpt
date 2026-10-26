# secpt

无人值守的安全测试智能体。给定目标清单或附件 → 无人值守执行 → 产出**带证据链、可复核**的报告。

> 仅面向**获得书面授权**的渗透测试、红队演练与靶场评测。不做无授权测试、不做自动发现目标、不自动执行利用 payload。

Python 3.11+，依赖仅 `pydantic` / `httpx` / `pyyaml`。CLI 命令 `secpt`，Python 包名 `pa`，环境变量前缀 `SECPT_`。

## 目录结构

```
secpt/
├── pa/                        # Python 包（短导入名）
│   ├── models.py              # 全部数据契约（pydantic）
│   ├── ports.py / errors.py / envs.py
│   ├── store/                 # 事件账本（append-only）+ 投影 + 合并
│   ├── graph/                 # 任务图 / 状态机 / ready 集合
│   ├── gateway/               # 动作网关：所有工具执行的唯一入口（8 步门）
│   ├── core/                  # 决策大脑：planner / worker / reflect / nl
│   │   ├── secrules.py        #   网络侧确定性规则（安全头/弱TLS/catch-all）
│   │   ├── filerules.py       #   附件侧确定性规则（密钥/危险sink/依赖CVE）
│   │   ├── prompts/           #   planner / orchestrator / worker 提示词
│   │   └── audit.py / experience.py / memory.py
│   ├── policy/                # 教练（off/shadow/enforce 三档）
│   ├── adjudicate/            # JEV 结果裁决（confirmed/suspected/false_positive）
│   ├── runtime/               # 回合驱动：loop / scheduler / budget / wakeup
│   ├── report/                # 交付：report / log / trace / stats / flow / tui
│   ├── tools/                 # 工具池：probe/http/tls/dns/shell/fs(+grep)/MCP
│   └── infra/                 # llm / evidence(内容寻址) / executor / fp
├── assets/                    # 运行资产（数据驱动，mission 级冻结）
│   ├── prompts→pa/core/prompts│   skills/ · roles/ · playbooks/（front matter markdown）
├── docs/                      # 设计与状态文档（见「文档地图」）
├── tests/                     # 验收测试
├── missions/                  # 运行时数据（gitignore）：账本/证据/报告
│   └── <family>/runs/N/
│       ├── events.jsonl       #   原始账本（append-only）
│       ├── analysis.md        #   ★ 运行分析：全过程+系统信号+失败归因+优化建议
│       └── workspace/         #   附件与工具工作区
├── <family>/report.md         # 交付物：验收核对矩阵 + findings + 证据链
├── pyproject.toml
└── README.md
```

## 快速开始

```bash
pip install -e .
cp .env.example .env          # 填 SECPT_BASE_URL / SECPT_API_KEY / SECPT_MODEL

# 典型工作流
secpt do '扫描 lake.aimohu.com，列出安全漏洞' --yes   # 一句话立项并执行
secpt status                                          # 全部 mission 一览
secpt log missions/do-xxx                             # 深入单个（--flow 重放流程叙事）
secpt report missions/do-xxx                          # 证据链报告 report.md

# 0day 研究（附件/文件）
secpt do '审计这个项目' --attachment ./src --yes      # 文件/目录入 workspace
# 引擎自动扫描：密钥 / 危险 sink / 依赖 CVE（SECPT_ADVISORIES 指向 OSV 兼容 JSONL）
# worker 用 fs.read/grep 复核可达性；findings 走 JEV 裁决
```

所有查看类命令（log/report/trace/stats/watch/tui/resume）接受**家族目录**并自动解析 `latest → runs/N`；同一任务重复 `do` 会新开 `runs/N`，互不覆盖。

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `do` | 一句话任务 → 解析确认（含验收合同）→ 实时流程叙事 → 收口 |
| `status` | mission 家族一览（runs/终态/tokens/findings），全局入口 |
| `resume` | 续跑家族最近一次 run（`--follow --flow` 可跟流程叙事） |
| `run` | 新目录引导（`--spec mission.json`）/ 已有目录续跑 |
| `log` | 离线叙事日志（编排决策/思考/裁决/收口；`--flow` 重放流程） |
| `report` | 证据链报告：STATUS 横幅 + 验收核对矩阵 + 资产暴露面 + 已排除/观察 |
| `trace` / `stats` | 结构化轨迹 / 跨 mission 聚合（confirmed/100k、解析失败率） |
| `watch` / `tui` | 只读跟随 / 交互式事件面板 |

## 机制速览

- **事件账本是唯一真相**（append-only + fsync + 幂等键）：kill -9 后 resume 与不中断跑逐字段一致；报告/日志/轨迹全部从账本确定性重放。
- **副作用唯一入口**是动作网关（范围校验 → 工具白名单 → 风险档 → 去重 → 执行 → 证据落盘 → 审计）；**结论唯一出口**是 JEV 裁决（confirmed/suspected/false_positive + 补证方向）。
- **编排者有护栏也有牙齿**：run/create/abandon 三权分立，判据合同门拒绝散文与伪证据，教练 advisory（thrashing/vs_rate）直接进编排上下文。
- **验收核对矩阵**：报告区分 VERIFIED / CONTRACT_CONFLICT / NOT_OBSERVED，同族线索存在不冒充"预期值已验证"；0 任务/合同冲突输出 NOT_EXECUTED / PARTIAL 故障态，不产出假"无发现"报告。
- **每次运行自动落盘 `runs/N/analysis.md`（运行分析）**：面向系统优化的全过程记录——运行元信息、系统信号（needs_help/缓存命中/编排降级/合同门拒绝/解析失败率）、具体到本次运行的优化建议、逐任务失败归因、完整思考-行动-观察叙事（与 `secpt log` 同源）。任意终态都写（失败的运行恰恰是最重要的分析素材）；`secpt report <dir>` 一键重新生成两份。

## 环境变量

一律 `SECPT_` 前缀，唯一读取入口在 `pa/envs.py`（业务代码不直接 getenv）：

| 变量 | 取值 | 用途 |
| --- | --- | --- |
| `SECPT_BASE_URL` | URL | 真实 LLM 端点（OpenAI 兼容） |
| `SECPT_API_KEY` | 密钥 | 真实 LLM 鉴权（只存私有属性，不出现在日志/repr/异常里） |
| `SECPT_MODEL` | 模型名 | 主模型 |
| `SECPT_FALLBACK_MODEL` | 模型名 | 降级目标（enforce 下 vs_rate 越线写入 model.switched） |
| `SECPT_LLM_MOCK` | JSON 脚本 | MockLLM 注入（测试/演示）；设置后忽略真实端点 |
| `SECPT_MENTOR_MODE` | `off`/`shadow`/`enforce` | 教练档位，默认 `shadow` |
| `SECPT_ADVISORIES` | JSONL 路径 | 依赖 advisory 库（OSV 兼容），未设置 = 无 1day 匹配 |
| `SECPT_MAX_CTX_CHARS` | 正整数 | user 上下文字符预算（默认 120000） |
| `SECPT_PROBE_TIMEOUT` | 秒 | probe 单连接超时天花板（默认 5.0） |
| `SECPT_EXPERIENCES` | JSONL 路径 | 跨 mission 经验库（缺省工作区级） |
| `SECPT_MCP_JSON` | JSON 或路径 | MCP 工具池声明（缺席 = 能力缺席） |
| `SECPT_DIE_AT_STEP` | 正整数 | 测试钩子：第 N 个超级步 `os._exit(9)` 模拟断电 |
| `SECPT_LOG_LEVEL` | DEBUG~ERROR | stderr 日志等级，默认 `WARNING` |

## 退出码

`0` completed（验收达成） / `2` budget（预算耗尽） / `3` plateau（平台期） /
`4` halted（熔断：deadline 或 interrupted） / `1` error（异常、failed、resume 不存在目录）

## 已知限制

- **planner-driven，无主控回合**：v0 只有引导时的一次计划 + 每轮编排 +
  worker 的有界 ReAct，CONTROL_PLANE 里的 core/decide 全局重规划回合
  未实现（`plan.revised` 通道在模型与事件层就绪，scheduler 尚未消费）。
- **策略未数据化、未版本化**：范围与策略来自 `mission.json` 文本，
  没有策略库、没有版本管理，同一 objective 两次运行可能拆出不同计划。
- **无跨客户隔离与检索过滤**：所有 mission 共用静态 `assets/` 目录，
  事件记忆（单 mission 账本）不跨 mission 检索。
- **无评测基线**：没有回归评测集，模型/提示词改动的效果无量化对照。
- **无经验→技能写回**：运行中产出的 lessons 不回流为技能资产。
- **spent_seconds 不可复现**：预算里的秒数按墙钟结算，中断恢复后无法与
  不中断跑法逐字段一致。因此 `budget.seconds` 只当保险丝（如租约过期
  判定），**不得作为主要终止判据**。默认终止看 `budget.tokens` /
  `budget.steps`，平台期看 `no_progress_steps`。
- 平台期收口区分原因（`meta["plateau_reason"]`）：`no_ready`（任务排空、
  验收未达成）/ `thrashing`（任务把 3 次尝试耗尽仍失败，无效重试）。

## 术语

- **事件记忆（短期记忆）**：`events.jsonl` 事件账本——单个 mission 内
  的唯一事实源，可随时快照+重放重建。
- **长期记忆**：跨 mission 的共享资产（经验库 `experiences.jsonl` +
  `assets/` 下的技能/角色/playbook）。经验在收口时自动提炼入库
  （带 origin/finish_reason 溯源），技能仍为人工维护；无跨客户隔离。

## 文档地图

设计文档在 `docs/`，是各阶段的**设计记录**；运行期布局与行为以本 README 与代码为准。

| 文档 | 内容 |
| --- | --- |
| `docs/ARCHITECTURE.md` | 分层 / 模块树 / 接口契约 / 状态机 |
| `docs/CONTROL_PLANE.md` | 判定矩阵 / 失败恢复 / 教练与 JEV / 记忆分层 |
| `docs/BUILD_SPEC.md` | v0 施工单（逐文件实现与验收） |
| `docs/WORKFLOW.md` | 工作流决议：双层控制环 / 黑板 / 信号级唤醒 / 对标（ARTEX、sec-agent） |
| `docs/ENTERPRISE_DESIGN.md` | **企业级长任务高复杂场景**：对标开源 harness 的差距分析与 M1–M6 演进设计 |
| `docs/DESIGN.md` | 早期架构设计说明（Hermes 对照） |
| `docs/STATUS.md` | 实现状态台账（每行 ✅ 对应一个真实测试，由 `tests/test_status.py` 保证） |
| `docs/REPORT_TEMPLATE.md` | 报告模板与写作纪律 |
