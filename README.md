# secpt

自主渗透/复杂任务 Agent harness。Python 3.12 + asyncio + pydantic-settings + structlog + typer。

> 作者：**一片丹心** ｜ 许可证：[Apache-2.0](LICENSE) ｜ 架构以 `docs/架构现状.md` 为准

## 快速开始（推荐主入口 solve）

```bash
export DEEPSEEK_API_KEY=<key>
secpt solve challenges/rois-saml-sso          # 一键：入口/附件/先验全自动 → 运行 → 评判 → 学习
secpt solve <dir> --prior saml --wall 1800    # 覆盖项仍可干预
secpt solve <dir> -p multistage               # 多阶段：recon → getshell → privesc
```

## 核心能力

- **统一理解智能体（solve 首环）**：一次 LLM 调用同时产出「打法分类（**多标签**，横向任务自动合并工具/先验并集）+ 结构化目标 GoalSpec + 先验推荐」，取代原「路由→拆解」两次调用；入口抽取/附件解压/白名单校验/种子入图照旧（`SECPT_UNDERSTANDING=0` 回退旧两段式）
- **一键解题（solve）**：入口 URL 抽取、附件自动解压、先验匹配（关键词→语义兜底）、跑完自动评判 + 经验沉淀
- **多智能体团队模式（frontier）**：意图队列（SQLite 持久化、按任务隔离、依赖/事实谓词解锁、资产级租约互斥）+ 四角色分工（侦察 / 利用×N 并行 / 验证 / 报告），失败自动复核一次，协作走事件流 + intel 图/`search_traces` 跨任务检索 + 完成广播；人机通道 intent add/set/rm
- **Benchmark 多轮 Campaign**（`profile=benchmark` 自动走）：难度感知步数预算 easy/medium/hard=40/60/110、多旗题按段拆分逐段结算、失败题自动重试（`SECPT_CAMPAIGN_ROUNDS`）、平台结束/期限到优雅排空（A3）；跨进程互斥账本 `ChallengeLedger` 防重复解题
- **进度锚点防失忆**：长任务 compaction 删除旧消息后，`engine` 每 5 步落盘 `.progress.md`（凭证脱敏、隐藏文件不污染 `ls` 观测），压缩/rollover 后作为"结构化进度摘要"重新注入，模型不会丢失"做到哪/试过什么/什么被否定"
- **Playbook 自动蒸馏 + 通用打法模板**（学习层增强）：
  - `learning/playbook.py` 族级打法自动蒸馏（确定性，从结算摘要按题族聚合"已验证/已否定路线"，注入 benchmark 意图 goal）
  - `learning/techniques.py` + `knowledge/techniques.yaml` 数据驱动通用打法模板（SSRF/SSTI/反序列化等信号匹配注入；新增打法只改 YAML）
- **Traffic 武器化**：`traffic/replay.py` 把流量层从"取证录制"升级为"攻击面复用"——`list_exchanges` 按 host 过滤、`replay` 重放并覆盖 method/header/body、`fuzz_param` 单参数逐 payload 差分；发送过 scope 校验 + TLS 默认校验
- **非阻塞 compaction**：主循环仅同步删组+插占位（微秒级），LLM 摘要交后台 worker（`secpt worker summarize` 补偿入口），`compaction_requested/completed` 事件全链路可观测
- **断点续跑（checkpoint resume）**：重启后从 `events.jsonl` 恢复轮次/预算/检测器状态，预算累计无缝衔接；仅顶层任务路径开启，编排层防串扰
- **多链并发安全**：EventSink 线程锁 + flock 文件锁 + 跨进程 seq 单调；工具调用按 `call_id` 精确配对（不再依赖 FIFO）；SQLite 写入 busy 指数退避重试；`runtime/workspace_ctx.py` ContextVar 隔离各任务工作区（frontier 多 worker 线程互不踩环境变量）
- **可观测性**：structlog JSON 日志（`SECPT_LOG_JSON=1`，按 job_id/agent 检索）+ 零依赖 Prometheus 指标（`secpt worker metrics --port 9100`）+ 18 类事件流 `events.jsonl`
- **越用越好用（learning 闭环）**：每次运行 LLM 蒸馏经验入 `var/knowledge/experiences.jsonl`，下次同类任务自动注入 top-3 历史经验（`SECPT_EXPERIENCE=0` 关闭）
- **工具插件（tools/plugins/）**：YAML 声明式集成外部工具/其他智能体能力（如 getshell），模板参数 shlex.quote 防注入；profile `tools:` 声明即装配。内置：
  - `web_search`（Tavily/Brave/SearXNG 三选一，服务型靶场"指纹→已知漏洞"打法）
  - `browser_*`（Playwright，系统 chromium 回退免 CDN）
  - `http_request` / `session` / `oob`（外带回调）/ `traffic`（流量武器化）/ `recon_parse`（侦察结果解析）
  - `crypto_helpers` / `analyze_binary` / `sanitize_oracle`（密码学/二进制/净化 oracle）
- **子智能体（delegate 工具）**：主链按需派生**一次性子智能体**（独立上下文、结论压缩回传、结构性禁递归、治理继承、子工作区挂靠父 ws）——市场共识形态（Claude Code Task / Deep Agents subagent）；`SECPT_DELEGATE=1` 开启。执行层保持单链，编排层按"清单型关规划（benchmark）/探索型开规划（hunt）"分流
- **多阶段渗透（profile stages）**：recon → getshell → privesc 串行编排，共享 workspace，阶段交付物自动交接
- **AI 评判（不依赖 flag 正则）**：`secpt judge <job_id> [--ground-truth FILE]`——反作弊 → Jev 门控 → LLM judge 终裁
- **7×24 长任务**：`--wall 0` 不限墙钟，compaction/rollover 上下文自愈，卡死切换器（六判据 + 进度锚点）自动纠偏，协作式取消；通用 `DEADLINE_TS` / `INTENT_WALL_S` 控制全局截止与意图墙钟
- **多任务**：`secpt submit -t spec.yaml -j 4 --priority 9` / `secpt forever`（进程池 + 优先级队列 + 重启恢复排队）
- **运维体验**：`secpt doctor`（环境预检，多模型凭证支持 DEEPSEEK_API_KEY/SECPT_MODEL_API_KEY/SECPT_MODEL_KEY_<PREFIX>）/ `secpt tui`（意图板+事件流+审批面板，pip install 'secpt[tui]'）/ 通知通道（SECPT_NOTIFY_WEBHOOK 或 SECPT_NOTIFY_DESKTOP=1 → 审批待办/空转暂停/拿到 flag 实时推送）
- **Docker Agent 镜像**：`scripts/build-agent-image.sh` 一键构建 Kali 基座自包含镜像（离线预装 50+ 安全工具 + Python 武器库 + 精选字典），`docker/entrypoint.sh` 启动即跑 benchmark；凭证全部来自运行时环境变量，镜像零密钥

## 架构分层（代码驱动，读一遍就能跑）

```
┌─────────────────────────────────────────────────────────────────────┐
│ 路由层 solve.py / dag.py  —— 决定走哪条路径（懒升级阶梯）            │
│   route_mode: profile.stages → stages / benchmark → campaign / 默认 single │
└────────────┬──────────────────┬────────────────────┬───────────────┘
             │                  │                    │
             ▼                  ▼                    ▼
  run_single_chain()     run_frontier()        run_benchmark_campaign()
  (dag.py)               (frontier.py)        (benchmark.py)
  ┌────────────────┐     ┌─────────────────┐  ┌──────────────────────┐
  │ attempt-1:     │     │ _worker_loop ×N │  │ for round in 1..3:   │
  │  to_thread(    │     │  claim → _run_one│  │   run_benchmark()    │
  │   run_engine)  │     │  → settle        │  │     ├─ ChallengeLedger│
  │ attempt-2:     │     │  (to_thread     │  │     ├─ chunk_ranges   │
  │  -r2 / -rs     │     │   run_engine)   │  │     └─ run_frontier   │
  │  RESET 重建    │     │ planner + retro │  │ 心跳线程 + 平台探针   │
  └───────┬────────┘     └────────┬────────┘  └───────────┬──────────┘
          │                       │                        │
          └───────────────┬───────┴────────────────────────┘
                          ▼
          run_engine(cfg)  ← 同步包装，内部 asyncio.run()
                          ▼
             Engine.run()  ← 单链 ReAct 主循环
  messages 整条 append（不 rebuild） ｜ tool_call 结果回填
  三档止损（nudge→wrap→stalled）     ｜ StuckDetector 六判据卡死检测
  非阻塞 compaction 压缩             ｜ checkpoint 断点续跑
  进度锚点 .progress.md（5 步落盘）  ｜ delegate 子智能体（可选）
```

### 关键设计决策

| 层 | 决策 | 为什么 |
|----|------|--------|
| **Engine 是同步包装** | `run_engine()` 是同步函数，内部 `asyncio.run(Engine(...).run())`；编排层用 `asyncio.to_thread(run_engine, ...)` 驱动 | 每个智能体执行有自己独立的 event loop，不污染编排层 event loop；线程池天然隔离 session 状态 |
| **Engine 不知道"重试/协作"** | 重试（attempt-2）、RESET 重建、跨意图 handoff 全在编排层 | Engine 是可复用 ReAct 循环，所有复杂行为在上层组合 |
| **没有 agent 直接对话** | 协作走 SQLite 意图队列 + SharedStopper 完成广播 + `_YieldHook` steer 消息 | 避免模型间相互误导；协作确定性由持久化存储保证 |
| **frontier.sqlite 按任务隔离** | `var/jobs/<base>/frontier.sqlite` | 并发任务互不可见；resume 同 base 续接同一 todolist |
| **run_single_chain 是默认** | `route_mode()` 默认返回 single | 懒升级阶梯——最小充分打法，短任务最优；无成果自动升级 frontier |

### 三种驱动模式速查

| 模式 | 触发 | 并发 | 协作 | 典型场景 |
|------|------|------|------|----------|
| single 单链（默认） | profile 无 stages / 非 benchmark | 1 个 Engine 实例 | 无；失败自动重试一次（`-r2` 续步 或 `-rs` RESET 换会话） | 单目标探索、7×24 长任务 |
| frontier 团队协作 | `--mode frontier` 或 solve 自动升级 | N 个 worker_loop（asyncio task），每个 `to_thread(run_engine)` | 意图队列 + 原子 claim + 租约互斥 + 事实谓词解锁 + 完成广播 steer | 多资产渗透、复杂任务 |
| benchmark campaign | profile=benchmark 自动走 | 3 worker 并行 + 外层多轮重试 | 同上 + ChallengeLedger 跨进程账本 | 多题跑分比赛 |

### 团队协作五角色（role_plan=True 时预置骨架）

| 角色 | 解锁条件 | 做什么 | 协作产物 |
|------|----------|--------|----------|
| **recon** | 无依赖，先行 | 枚举资产/端口/技术栈 → `ws/assets.json` + `record_fact("credential:xxx")` | 写入 intel 图 → 解锁 exploit / lateral |
| **exploit** | `depends_on=recon_id` | 定向深挖单个资产 | 每资产一条，**多 worker 并行认领** + 租约互斥 |
| **lateral** | `depends_on="fact:credential"`（intel 图有凭据） | 凭据复用/横向移动 | 不靠 LLM 自觉，`deps_satisfied()` 确定性判定 |
| **verify** | planner 看到 exploit 有未确认 finding 时补产 | 独立 PoC 复现 | 执行完 finding 才进 confirmed |
| **report** | verify 全部完成（确定性聚合，**不烧 LLM 步数**） | 聚合 confirmed finding 产报告 | — |

## 插件示例（集成 getshell 能力）

```yaml
# tools/plugins/getshell.yaml
name: getshell
description: 对目标执行 getshell 尝试
schema: {type: object, properties: {target: {type: string}}, required: [target]}
run: "getshell --target {target} --json"   # 换成你环境里真实的工具 CLI
timeout_s: 300
```

## 快速开始（唯一入口 solve）

```bash
# 1. 准备环境
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export DEEPSEEK_API_KEY=<key>          # 或任意 OpenAI 兼容端点（见 docs/架构现状.md 模型路由）

# 2. 一键执行——任务类型与打法都不需要用户判断：
secpt solve challenges/rois-saml-sso
#    内部自动：统一理解智能体（多标签分类 + GoalSpec + 先验推荐，一次调用）
#    → 白名单校验/种子入图 → 多标签合并（横向任务工具+先验并集）
#    → 执行模式（路由决策，详见"架构分层"）：
#      benchmark profile → run_benchmark_campaign（多 worker 并行 + 多轮重试）
#      profile 带 stages → run_stages（recon→getshell→privesc 串行）
#      --mode frontier → run_frontier(role_plan=True)（五角色团队协作）
#      其余 → run_single_chain（最简路径，失败自动重试一次）
#    → AI 评判 → 经验沉淀。高级用户可用 -p/--prior/--mode 覆盖任何一步。

# 3. 无 key 干跑（FakeLLM 全链路）
SECPT_FAKE_LLM=1 secpt solve challenges/rois-saml-sso

# 实时观察（另开终端）：逐步思考/工具调用/响应/发现/压缩
secpt watch <job_id>

# 4. 测试与规范
make check                             # ruff check + ruff format --check + pytest（提交前必跑）
make recheck REPORT=<report.json>      # 可选：复跑判据门禁
```

> 🚀 **想快速读懂整个系统？→ [docs/系统速览.md](docs/系统速览.md)**（5 分钟版）
> 开发约定与架构红线见 [AGENTS.md](AGENTS.md)；文档导航见 [docs/README.md](docs/README.md)；
> 权威架构描述见 [docs/架构现状.md](docs/架构现状.md)。

## 配置

**环境变量是唯一配置源**（`config.yaml` 已移除）：复制 `.env.example` 为 `.env` 填入
`DEEPSEEK_API_KEY` 即可；全部可调项见 `.env.example`（模型路由/请求参数/沙箱/人在环路）。
优先级：真实 `export` > `.env` 文件 > 内置默认。

## 目录约定

```
secpt/
├── AGENTS.md                  # 项目约定（构建/测试/风格/红线）——修改前必读
├── LICENSE                    # Apache-2.0
├── Makefile                   # make check = lint + format-check + test
├── pyproject.toml             # ruff(line-length 100, py312) / pytest / coverage
├── .env.example               # 全部可调项（唯一配置源：SECPT_* 环境变量）
├── src/secpt/
│   ├── cli/                   # typer 薄注册层：app / solve(★主入口) / run / resume / watch / doctor / model-check
│   ├── runtime/               # engine.py(★主循环 + 进度锚点 .progress.md) / llm client(多模型路由+池)
│   │                          #   sandbox(Docker沙箱) / logsetup / metrics / workspace_ctx(ContextVar 隔离各任务)
│   ├── core/
│   │   ├── job/               # 作业层：TaskSpec/JobRecord/store/event(并发安全)/resume/summarize_worker/worker
│   │   ├── orchestrator/      # ★编排层（驱动 Engine 的三种模式）
│   │   │   ├── dag.py         # run_single_chain + run_task 分流 + goal 拆解层
│   │   │   ├── frontier.py    # 意图调度（五角色骨架 + _worker_loop×N 并行 + SharedStopper + 跨意图 handoff）
│   │   │   ├── benchmark.py   # 多轮 campaign（run_benchmark_campaign 外层重试 + run_benchmark 内层跑 frontier）
│   │   │   ├── ledger.py      # ChallengeLedger 跨进程互斥账本（benchmark 场景防重复解题）
│   │   │   ├── roles.py       # 角色定义 + role_stage 提示注入（recon/exploit/lateral/verify/report）
│   │   │   ├── stages.py      # 多阶段串行（recon→getshell→privesc）
│   │   │   ├── policy.py      # 路由 + prove_goal + DEPRECATED_MODULES 红线
│   │   │   └── goals.py       # 目标拆解 + asset_scope 结构化（render_goal_system）
│   │   ├── profile/           # profile YAML loader + system_extra 渲染（benchmark/hunter/pentest/redteam…）
│   │   ├── context/           # S3 四件套：compaction(非阻塞) / rollover / budget / summarize
│   │   ├── memory/            # warroom 四表（历史/可选组件；多意图协作走 intel 图 + findings + traces）
│   │   └── watchdog.py        # 空转监控（卡死切换器六判据 + 进度锚点）
│   ├── governance/            # scope / ratelimit / intercept / audit（审计 JSONL）
│   ├── evaluation/            # AI 评判层（anticheat→Jev→judge→verdict）
│   ├── findings/              # 漏洞发现全生命周期（验证纪律/CWE/CVE 去重）
│   ├── intel/                 # 图记忆（资产图+血缘图+覆盖度+冷压缩）
│   ├── traffic/               # harness + recorder + store + replay(武器化：重放+单参数变异)
│   ├── report/                # CWE 映射 + 报告交付（md/md-zip 证据包）
│   ├── learning/              # 经验闭环：distill / store / retrieve + playbook(族级自动蒸馏) + techniques(YAML 数据驱动打法注入)
│   ├── tools/                 # plugin_loader（plugins/ 外部能力集成）
│   ├── challenge/             # 挑战加载（题面/入口/附件，solve 的原材料）
│   └── knowledge/             # priors/ 先验打法 + prior_map.yaml 路由表 + techniques.yaml 通用打法模板
├── tools/plugins/             # 用户插件（YAML 声明式 / Python）。内置：web_search / browser / http_request / session / oob / traffic / recon_parse / crypto_helpers / analyze_binary / sanitize_oracle / getshell(YAML) / pwn(YAML)
├── docker/                    # agent.Dockerfile（Kali 基座比赛镜像）/ sandbox.Dockerfile / sandbox-pwn.Dockerfile / entrypoint.sh / task-template.md
├── scripts/                   # build-agent-image.sh / prepare-image-seed.py / recheck.py / archive_runtime.py / 实验验证脚本
├── seed/knowledge/            # 构建时注入镜像的脱敏知识种子（experiences.jsonl + playbooks/*.md）
├── tests/unit/**              # FakeLLM 为地基，无 key 全链路可测；覆盖 benchmark campaign / frontier / roles / learning / 评判 / M4 工具…
├── docs/                      # 架构现状(权威) + 系统速览(5分钟版) + 实验报告 + 各设计文档
├── logs/                      # 运行日志归档（gitignored）
└── var/                       # 运行时数据（jobs/knowledge/audit…，gitignored）
```

## Docker Agent 镜像（比赛/无环境场景）

```bash
# 1. 构建（自动生成 seed/knowledge 脱敏种子 + 打包 Kali 基座 + secpt wheel）
bash scripts/build-agent-image.sh
# 输出：secpt-agent:latest（约 3-4GB，含 50+ 安全工具 + Python 武器库 + playwright）

# 2. 本地冒烟
docker run --rm \
  -e DEEPSEEK_API_KEY=sk-xxx \
  -e BENCHMARK_TOKEN=xxx \
  -e BENCHMARK_BASE_URL=http://benchmark.example.com \
  secpt-agent:latest
# entrypoint.sh 会自动拉题 → 三 worker 并行解题 → 失败重试 → 提交 → 优雅退出
```

镜像零密钥（API key 从运行时环境变量注入）、离线预装（Kali 源全量 apt + 清华镜像加速）、精选字典（不装 1.85GB seclists）。详见 `docker/agent.Dockerfile` 注释。

## 许可证

本项目基于 [Apache License 2.0](LICENSE) 开源。

Copyright (c) 2025-2026 **一片丹心**
