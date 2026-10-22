# secpt

自主渗透/复杂任务 Agent harness。Python 3.12 + asyncio + pydantic-settings + structlog + typer。

> 作者：**一片丹心** ｜ 许可证：[Apache-2.0](LICENSE) ｜ 架构以 `docs/架构现状.md` 为准

## 快速开始（推荐主入口 solve）

```bash
export DEEPSEEK_API_KEY=<key>
secpt solve challenges/rois-saml-sso          # 一键：入口/附件/先验全自动 → 运行 → 评判 → 学习
secpt solve <dir> --prior saml --wall 1800    # 覆盖项仍可干预
secpt solve <dir> -p multistage               # 多阶段：recon → getshell → privesc（折叠为意图链）
```

## 核心能力

- **入口协议闸口 + 任务理解归指挥官**：入口只做一件事——题面命中 benchmark 平台关键词即升级战役跑分（唯一协议检测）；**任务理解、目标拆解、角色/场景/经验选择全部归指挥官首轮唤醒**——GoalSpec（落盘 goal.json，resume 复用）+ 从角色目录选 profile（含场景指引，落盘 role.json）+ 从经验目录选 priors，直接赋予操作员。入口不再替 AI "理解任务"（`SECPT_GOALS=0` 回退 legacy 入口拆解）
- **一键解题（solve）**：入口 URL 抽取、附件自动解压、先验匹配（关键词→语义兜底）、跑完自动评判 + 经验沉淀
- **单一执行编排（frontier）**：指挥官产**彼此独立、可并行的任务**（带角色/截止/注意事项）；triage/JEV 快判把下载、批量搜索、爆破、爬取等耗时重复工作分流到后勤后台通道，不阻塞主路径；操作员 ReAct 并行认领；交付门在收口前核验战果、必要时补一条独立复验任务；收敛由指挥官裁决（见 v4 条）；教练在重试/RESET/卡壳时注入确定性纠偏；收尾写 `retro.md` 复盘。旧五角色骨架保留为 `SECPT_ROLE_TEMPLATES=1` legacy。
- **执行体分层**：指挥官 CDR（方向/拆分/派单/收敛裁决/质量反馈综合裁决）、值守哨兵 SNT（结算+触发器）、执行智能体即操作员（通用 ReAct，任务池原子领取，按领到的角色/技能工作）、后勤 QM（后台下载/批量搜索/长耗时收集，`delegate_logistics` 异步委托）、交付核验 VRF（质量工程师：证据/过程核验、独立复验，只评质量不拍板——否决经 quality 信号报指挥官裁决）、教练 COH（卡壳介入：提示/换路径/缩任务/升级/重派）。智能体间不直接对话，协作经任务池/作战地图（frontier.sqlite + hypothesis.sqlite + strategy.md + events.jsonl）。
- **工作循环 v4（值守层 + Commander + 假设树）**：值守层（纯代码，常驻）结算一切事件并按确定性触发器唤醒主智能体——预算里程碑（30/50/75%）、停滞（无信念增量 ≠ 无产出）、巩固定时器、**地图质变（confirmed 假设/真实 flag 变化当轮插队，不再等定时器）**；多信号同轮攒下**一次唤醒全交付**（防信号饿死）。Commander 无会话状态，每次唤醒读确定性简报（假设树 + 战果 + 在途任务 + 触发议题），做信念结算与方向决策，全量重写战略文档（防漂移闸：大转向必须留论证）；假设树状态机 proposed→testing→confirmed/refuted（结论必须证据锚定）。**收敛裁决归指挥官**：值守层两处自然收敛只递 convergence 信号不拍板，由指挥官读图裁决 deliver 或补产；连续两次不交付才回退兜底收口。`SECPT_ORCH_COMMANDER=0` 回退 v3。
- **派单三要素（指挥官→操作员）**：每个任务带 `max_wall_s`（何时截止，到点即结算）、`notes`（注意事项：边界/风险/禁止动作）、`self`（简单任务指挥官当场自执行，不占操作员名额）；派单可逐任务带 profile/prior——同一 engagement 的不同操作员可领不同角色（如 web+pwn 混合任务）。操作员目标编译注入【当前主攻方向】（战略文档摘录）与【已验证漏洞·跨资产情报】（复用而非重挖）
- **角色/场景/经验目录化**：8 个可选 profile（assessment/codeaudit/crypto/ctf/hunter/pentest/pwn/redteam/**web**）各带 `scenario` 场景指引（何时选我/典型目标/边界），`role_catalog()` 三元组 + `prior_catalog()` 经验目录注入简报——指挥官角色选择有据可依，选择结果按任务赋予操作员（intents `profile`/`prior` 列，内部档案 benchmark 等不可选）
- **技能动态发现（ZCode 式）**：`skills/<name>/SKILL.md`（frontmatter：description/when_to_use）即插即用——`discover_skills()` 目录注入指挥官简报，派单 `skills` 数组按任务挂载（intents `skills` 列，非法名静默过滤），操作员目标编译注入【技能包】正文；目录源 = 仓库 `skills/` + `SECPT_SKILLS_DIRS`（`:`/`;` 分隔，后写覆盖先写）+ `var/skills/`
- **编排模式唯一裁决**：`resolve_orch_mode()` 归约全部编排开关为一个显式模式（stages 清单 > role_plan 团队 legacy > 指挥官 > legacy），歧义组合运行时告警（如 `SECPT_ORCH_COMMANDER=0` 已弃用提示将在后续版本移除）；frontier 入口打印当前模式与告警，行为不再依赖散落的 env 判断
- **多任务并行防互撞**：全局目标租约 `var/leases/`（O_EXCL 原子文件 + TTL 自动过期，advisory 语义）——同一目标被并行 engagement 同时开打时，后到任务告警并建议限速避让，不硬阻断；任务收口自动释放全部租约
- **质变扩面 + 经验回灌**：地图质变信号从 confirmed 假设/真实 flag 扩展到**新 findings 落库**（只数本 engagement，并行任务的新发现不误触发）；指挥官简报增【历史经验提示】——按命中率（命中×2 + 与目标关键词重叠）Top-3 回灌，学习闭环回到决策层
- **产物自动压缩**：任务收尾自动执行（`SECPT_COMPRESS=0` 关）——events.jsonl 超 500 行轮转归档（`archive/events-<ts>.jsonl.gz`，seq 桥接标记 + `watermark.json` 保单调续接，Web 事件流游标回读自动补归档段）、frontier/hypothesis sqlite `VACUUM`、大日志 gzip；发布/搬运 job 目录体积可控
- **Benchmark 多轮 Campaign**（`profile=benchmark` 自动走）：难度感知步数预算 easy/medium/hard=40/60/110、多旗题按段拆分逐段结算、失败题自动重试（`SECPT_CAMPAIGN_ROUNDS`）、平台结束/期限到优雅排空（A3）；跨进程互斥账本 `ChallengeLedger` 防重复解题
- **进度锚点防失忆**：长任务 compaction 删除旧消息后，`engine` 每 5 步落盘 `.progress.md`（凭证脱敏、隐藏文件不污染 `ls` 观测），压缩/rollover 后作为"结构化进度摘要"重新注入，模型不会丢失"做到哪/试过什么/什么被否定"
- **Playbook 自动蒸馏 + 通用打法模板**（学习层增强）：
  - `learning/playbook.py` 族级打法自动蒸馏（确定性，从结算摘要按题族聚合"已验证/已否定路线"，注入 benchmark 意图 goal）
  - `learning/techniques.py` + `knowledge/techniques.yaml` 数据驱动通用打法模板（SSRF/SSTI/反序列化等信号匹配注入；新增打法只改 YAML）
- **Traffic 武器化**：`traffic/replay.py` 把流量层从"取证录制"升级为"攻击面复用"——`list_exchanges` 按 host 过滤、`replay` 重放并覆盖 method/header/body、`fuzz_param` 单参数逐 payload 差分；发送过 scope 校验 + TLS 默认校验
- **非阻塞 compaction**：主循环仅同步删组+插占位（微秒级），LLM 摘要交后台 worker（`secpt worker summarize` 补偿入口），`compaction_requested/completed` 事件全链路可观测
- **断点续跑（checkpoint resume）**：重启后从 `events.jsonl` 恢复轮次/预算/检测器状态，预算累计无缝衔接；仅顶层任务路径开启，编排层防串扰
- **多链并发安全**：EventSink 线程锁 + flock 文件锁 + 跨进程 seq 单调；父事件流三写入方（编排 `_parent_event`/Web 注入/聚合器）统一 PEV_LOCK 全局串行（seq 读-写跨写入方不再撞重号）；工具调用按 `call_id` 精确配对（不再依赖 FIFO）；SQLite 写入 busy 指数退避重试；`runtime/workspace_ctx.py` ContextVar 隔离各任务工作区（frontier 多 worker 线程互不踩环境变量）
- **可观测性**：structlog JSON 日志（`SECPT_LOG_JSON=1`，按 job_id/agent 检索）+ 零依赖 Prometheus 指标（`secpt worker metrics --port 9100`）+ 18 类事件流 `events.jsonl`
- **越用越好用（learning 闭环）**：每次运行 LLM 蒸馏经验入 `var/knowledge/experiences.jsonl`，下次同类任务自动注入 top-3 历史经验（`SECPT_EXPERIENCE=0` 关闭）
- **工具插件（tools/plugins/）**：YAML 声明式集成外部工具/其他智能体能力（如 getshell），模板参数 shlex.quote 防注入；profile `tools:` 声明即装配。内置：
  - `web_search`（Tavily/Brave/SearXNG 三选一，服务型靶场"指纹→已知漏洞"打法）
  - `browser_*`（Playwright，系统 chromium 回退免 CDN）
  - `http_request` / `session` / `oob`（外带回调）/ `traffic`（流量武器化）/ `recon_parse`（侦察结果解析）
  - `crypto_helpers` / `analyze_binary` / `sanitize_oracle`（密码学/二进制/净化 oracle）
- **子智能体（delegate / 后勤委托）**：主链按需派生**一次性子智能体**（`delegate`，独立上下文、结论压缩回传、结构性禁递归）；执行体遇到下载/批量搜索等耗时重复工作时可调用 **`delegate_logistics`** 入后勤后台队列并立即继续，不阻塞主路径。`SECPT_DELEGATE=1` 开子智能体；后勤由 `SECPT_ORCH_LOGISTICS=0` 关闭
- **多阶段渗透（profile stages）**：recon → getshell → privesc 折叠为 frontier 线性意图链（depends_on 串行 + 同资产产物交接），阶段交付物自动交接；`workers` 字段数据驱动并发。该线性清单语义不叠加 v3 交付/后勤通道
- **AI 评判（不依赖 flag 正则）**：`secpt judge <job_id> [--ground-truth FILE]`——反作弊 → Jev 门控 → LLM judge 终裁
- **7×24 长任务**：`--wall 0` 不限墙钟，compaction/rollover 上下文自愈，卡死切换器（六判据 + 进度锚点）自动纠偏，协作式取消；通用 `DEADLINE_TS` / `INTENT_WALL_S` 控制全局截止与意图墙钟
- **多任务**：`secpt submit -t spec.yaml -j 4 --priority 9` / `secpt forever`（进程池 + 优先级队列 + 重启恢复排队）
- **Web 控制台**：`secpt web --port 8080` → 浏览器版指挥台（http://127.0.0.1:8080）——**对话即任务流**：对话面板按 OpenAI chat 样式渲染（右侧用户气泡、左侧按智能体分组的运行交互：操作员思考/工具卡片/任务结算/指挥官规划/flag 里程碑），**对话中可随时发布任务**（消息=人工直投意图入当前作战地图；任务收敛后发送自动续接编排——同一张地图，不新开任务）；右栏**作战地图面板**：主攻方向/总目标/进度条/假设态势/执行中/待执行分组，产出成果按当前任务过滤；另有资产（intel 图谱）、发现（findings 分级卡片）、工具（内置/插件/打法技能）、经验（沉淀 + 使用统计）、配置（模型/编排开关运行时覆盖）。零新增依赖：stdlib HTTP + 轮询事件流（seq 游标增量，游标早于在线文件首 seq 时自动回读轮转归档段），默认只绑 127.0.0.1
- **运维体验**：`secpt doctor`（环境预检，多模型凭证支持 DEEPSEEK_API_KEY/SECPT_MODEL_API_KEY/SECPT_MODEL_KEY_<PREFIX>）/ `secpt tui`（意图板+事件流+审批面板，pip install 'secpt[tui]'）/ 通知通道（SECPT_NOTIFY_WEBHOOK 或 SECPT_NOTIFY_DESKTOP=1 → 审批待办/空转暂停/拿到 flag 实时推送）
- **Docker Agent 镜像**：`scripts/build-agent-image.sh` 一键构建 Kali 基座自包含镜像（离线预装 50+ 安全工具 + Python 武器库 + 精选字典），`docker/entrypoint.sh` 启动即跑 benchmark；凭证全部来自运行时环境变量，镜像零密钥

## 架构分层（代码驱动，读一遍就能跑）

```
┌─────────────────────────────────────────────────────────────────────┐
│ 路由层 solve.py / dag.py —— 统一执行路由（S6 模式收敛）              │
│   benchmark profile → run_benchmark_campaign；其余 → run_frontier   │
│   （单一执行编排；stages 折叠为线性意图链，workers 数据驱动）         │
└────────────┬───────────────────────────────┬───────────────────────┘
             │                               │
             ▼                               ▼
     run_frontier()              run_benchmark_campaign()
     (frontier.py · 唯一执行编排)  (benchmark.py · 平台协议层)
     ┌────────────────────┐      ┌──────────────────────────────┐
     │ 清单型：stages 种子 │      │ for round in 1..3:           │
     │  链（多阶段折叠）    │      │   run_benchmark()            │
     │ 探索型：planner 产  │      │     ├─ ChallengeLedger       │
     │  独立通用任务；     │      │     ├─ chunk_ranges          │
     │  triage → main /    │      │     └─ run_frontier          │
     │  logistics 双通道； │      │ 心跳线程 + 平台探针          │
     │  _worker_loop ×N +  │      └──────────────┬───────────────┘
     │  后勤 worker；      │                     │
     │  交付门核验；CDR 裁决收敛 │                     │
     │  教练介入 / retro.md │                     │
     └─────────┬──────────┘                     │
               └───────────────┬────────────────┘
                               ▼
               run_engine(cfg)  ← 同步包装，内部 asyncio.run()
                               ▼
                  Engine.run()  ← 单链 ReAct 主循环
  messages 整条 append（不 rebuild） ｜ tool_call 结果回填
  三档止损（nudge→wrap→stalled）     ｜ StuckDetector 六判据 + 教练/导师纠偏
  非阻塞 compaction 压缩             ｜ checkpoint 断点续跑
  进度锚点 .progress.md（5 步落盘）  ｜ delegate / delegate_logistics（可选）
```

### 关键设计决策

| 层 | 决策 | 为什么 |
|----|------|--------|
| **Engine 是同步包装** | `run_engine()` 是同步函数，内部 `asyncio.run(Engine(...).run())`；编排层用 `asyncio.to_thread(run_engine, ...)` 驱动 | 每个智能体执行有自己独立的 event loop，不污染编排层 event loop；线程池天然隔离 session 状态 |
| **Engine 不知道"重试/协作"** | 意图复核、RESET 重建、跨意图 handoff 全在编排层 | Engine 是可复用 ReAct 循环，所有复杂行为在上层组合 |
| **没有 agent 直接对话** | 协作走 SQLite 意图队列 + SharedStopper 完成广播 + `_YieldHook` steer 消息 | 避免模型间相互误导；协作确定性由持久化存储保证 |
| **frontier.sqlite 按任务隔离** | `var/jobs/<base>/frontier.sqlite` | 并发任务互不可见；resume 同 base 续接同一 todolist |
| **唯一执行编排 = frontier（S6→v3）** | 原 single/stages/团队三种模式收敛为 frontier 意图驱动：单资产=planner 自由规划；多资产=通用独立任务并行（不再预置角色骨架）；profile.stages=线性 depends_on 意图链；workers 由 profile YAML 数据驱动。v3 增加 main/logistics 双通道、triage 快判、交付门、教练与 retro | 单链是 frontier max_workers=1 的退化形态；v3 用"任务独立 + 按需后勤 + 交付判定"替代角色模板，任务不必先有角色才能执行 |

### 执行编排速查（S6→v3：只有一种执行编排）

| 调用形态 | 触发 | 语义 |
|----------|------|------|
| frontier 探索型 | 默认（profile 无 stages） | planner 产彼此独立的通用任务；triage 分流 main/logistics；执行体 ReAct 并行；交付门核验战果并决定结束；教练在重试/RESET 介入；收尾写 `retro.md` |
| frontier 清单型 | profile.stages 非空（多阶段折叠） | stages → 线性 depends_on 意图链，planner 关闭，全链跑完；不叠加 v3 后勤/交付通道 |
| benchmark campaign | profile=benchmark | 平台协议层（拉题/结算/账本防双开），内部即 run_frontier；后勤/交付门自动关闭以保持跑分语义 |

### 执行体与监督角色（v3→v10）

| 名称 | 触发 / 通道 | 做什么 |
|------|-------------|--------|
| **指挥官 CDR** | 值守层信号唤醒（事件驱动，不在场） | 任务理解+拆分、角色/场景/经验/技能选择与赋予、方向决策、战略文档全量重写、**收敛裁决**（deliver 或补产）与**质量反馈综合裁决**（VRF 否决经 quality 信号送达，修复/坚持/转向由 CDR 拍板）；简单任务自执行（self）不占操作员名额 |
| **值守哨兵 SNT** | 常驻，纯代码零 LLM | 结算一切事件；四类确定性触发器（预算/停滞/巩固定时器/地图质变[confirmed 假设/真实 flag/新 findings]）+ 收敛信号递交；连续两次不交付回退兜底收口；收尾写 retro.md |
| **执行智能体（操作员 A/B/C…）** | main lane，任务池原子认领 | 通用 ReAct：Thought → Action → Observation；任务之间无依赖，完成再领取下一个；按派单角色（profile/prior）与技能包（skills）工作 |
| **后勤智能体 QM** | logistics lane（后台 worker） | 接收 `delegate_logistics` 异步委托：下载工具/依赖、批量搜索、字典/爆破、爬取、长耗时收集；不阻塞主路径，结果回写任务池 |
| **交付智能体 VRF** | 交付门（收口时） | 质量工程师：核对证据链/独立复现，只评交付物质量不拍板——否决以 quality 信号穿透交付等待送达 CDR，复验任务入池推进尝试计数（耗尽仍未过 → 信念回滚 + 按未验证收口） |
| **教练 COH** | 卡壳/重试/RESET | 确定性纠偏：给提示、换路径、缩任务、升级/重派；只介入不抢决策权；Engine 侧另有可选导师模型（`SECPT_MENTOR=1`） |
| **JEV / triage** | planner 每轮 / 交付快检 | 秒级判断：任务分级、可行性/风险、main vs logistics、结果快检/拦截；规则优先，可选外部 JevGate（`SECPT_ORCH_JEV=1`） |

> 旧五角色模板（recon/exploit/lateral/verify/report）保留为 legacy：`SECPT_ROLE_TEMPLATES=1` 时恢复角色 planner 与 role_stage 注入；默认关闭。


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
#    内部自动：入口协议闸口（benchmark 关键词 → 战役跑分，唯一协议检测）
#    → 指挥官首轮唤醒：任务理解产 GoalSpec（落盘）+ 从角色/场景目录选
#      profile + 从经验目录选 priors → 拆分派单（带 max_wall_s/notes/self）
#    → 执行编排（唯一执行编排 = frontier 意图驱动，详见"架构分层"）：
#      benchmark profile → run_benchmark_campaign（多 worker 并行 + 多轮重试）
#      profile 带 stages → 折叠为线性意图链（recon→getshell→privesc 串行交接）
#      其余 → 操作员 A/B/C…按角色并行认领；值守层信号驱动指挥官调整方向
#             后勤后台不阻塞；收敛由指挥官裁决；交付门核验；教练干预卡壳
#    → AI 评判 → 经验沉淀。高级用户可用 -p/--prior 覆盖角色与经验选择。

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
│   │   ├── job/               # 作业层：TaskSpec/JobRecord/store/event(并发安全)/evlock(父流跨写入方锁)
│   │   │                      #   /resume/summarize_worker/worker
│   │   ├── orchestrator/      # ★编排层（S6→v3 唯一执行编排 = frontier 意图驱动）
│   │   │   ├── dag.py         # 统一路由：benchmark → campaign；其余 → frontier（augment_objective 拼接）
│   │   │   ├── frontier.py    # 意图调度（planner 产独立任务 / stages 折叠 / main+logistics 双通道
│   │   │   │                  #   + _worker_loop×N + 交付门 + 教练注入 + retro.md）
│   │   │   ├── hypothesis.py  # 假设树（Commander 世界模型：状态机+置信度+证据锚定）
│   │   │   ├── watch.py       # 值守层：失败类型化 + 确定性触发器（预算/停滞/内务）
│   │   │   ├── commander.py   # Commander：无状态唤醒会话 + 战略文档 + 防漂移闸 + 简报编译器
│   │   │   │                  #   （含技能目录/历史经验命中率回灌简报）
│   │   │   ├── modes.py       # 编排模式唯一裁决 resolve_orch_mode（stages/团队legacy/指挥官/legacy + 告警）
│   │   │   ├── global_lease.py # 全局目标租约（var/leases O_EXCL+TTL，跨 engagement 防互撞，advisory）
│   │   │   ├── triage.py      # JEV 风格快判：任务分级 / 风险 / main vs logistics / 结果快检
│   │   │   ├── coach.py       # 卡壳教练：重试/RESET 时给提示、缩任务、换路径
│   │   │   ├── delivery_gate.py # 交付门：证据核验、独立复验任务、结束判定
│   │   │   ├── benchmark.py   # 多轮 campaign（run_benchmark_campaign 外层重试 + run_benchmark 内层跑 frontier）
│   │   │   ├── ledger.py      # ChallengeLedger 跨进程互斥账本（benchmark 场景防重复解题）
│   │   │   ├── roles.py       # legacy 角色定义 + role_stage（SECPT_ROLE_TEMPLATES=1 才启用）
│   │   │   ├── policy.py      # prove_goal 量化验收 + DEPRECATED_MODULES 红线（单链/多阶段执行器已退役）
│   │   │   └── goals.py       # 目标拆解 + asset_scope 结构化（render_goal_system）
│   │   ├── profile/           # profile YAML loader + system_extra 渲染（benchmark/hunter/pentest/redteam…）
│   │   ├── context/           # S3 四件套：compaction(非阻塞) / rollover / budget / summarize
│   │   ├── sustain/           # 产物压缩：compress（events 轮转归档保 seq + sqlite VACUUM + 日志 gzip）
│   │   ├── skills.py          # 技能动态发现：skills_dirs/discover_skills/valid_skill/load_skill（后写覆盖）
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
│   ├── knowledge/             # priors/ 先验打法 + prior_map.yaml 路由表 + techniques.yaml 通用打法模板
│   └── web/                   # 指挥台：server(stdlib HTTP) / api(payload 构造+对话续接) / static(对话即任务流 UI)
├── tools/plugins/             # 用户插件（YAML 声明式 / Python）。内置：web_search / browser / http_request / session / oob / traffic / recon_parse / crypto_helpers / analyze_binary / sanitize_oracle / getshell(YAML) / pwn(YAML)
├── skills/                    # 技能目录（ZCode 式动态发现：SKILL.md + frontmatter，按任务挂载给操作员）
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
