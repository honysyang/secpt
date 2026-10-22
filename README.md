# secpt

自主渗透/复杂任务 Agent harness。Python 3.12 + asyncio + pydantic-settings + structlog + typer。

> 作者：**一片丹心** ｜ 许可证：[Apache-2.0](LICENSE) ｜ 架构以 `docs/架构现状.md` 为准

## 一图看懂：一次任务的完整流程

```
① 接入        secpt solve <dir> │ Web 对话面板发消息
   Gateway    附件解压 / 先验匹配——入口不做任务理解，原样交给指挥官
      │
      ▼
② 首轮唤醒    受理《接战单》（Web 确认后）→ 任务理解：GoalSpec → goal.json（resume 复用）
   CDR        角色/场景/经验/技能选择与赋予 → role.json + priors
              假设树 v0 + 战略文档 v0 + 拆分派首批任务（三要素：截止/注意事项/自执行）
      │  （执行效果不好时 CDR 会重新调整角色/场景/经验/技能，不只有一次）
      ▼
③ 稳态循环    操作员 A/B/C… 并行领单 ReAct ──产出──▶ SNT 结算 → 作战地图
  （占 90%）   ├─ 无触发器：CDR 不在场，循环继续（不烧 token）
              ├─ 地图质变（新 flag / confirmed 假设 / 新 findings）→ 当轮插队唤醒 CDR
              └─ 值守触发器（预算/停滞/收敛）→ 唤醒 CDR：信念结算 → 方向决策
                  → 必要时重选角色/场景/经验/技能 → 派单 → 重写战略文档 → 休眠
      │
      ▼
④ 收敛交付    SNT 只递 convergence 信号不拍板 → CDR 读图裁决（deliver 或补产；连续两次不交付 SNT 兜底收口）
              CDR 请求交付 → SNT 排空在途后勤 → VRF 核验（质量工程师，只评不拍板）
              ├─ 通过 → verified_flags 落账，结束
              └─ 否决 → quality 信号穿透交付等待回 CDR 综合裁决（修复/坚持/转向），复验任务入池
      │
      ▼
⑤ 收口归档    retro.md 复盘 → 学习层蒸馏经验（下次同类任务自动回灌）
              事件流轮转归档（保 seq）+ sqlite VACUUM + 日志打包 → job 目录可直接发布/搬运
```

**读图要点**：指挥官 CDR 决定"打哪里、用什么角色/技能打、什么时候结束"——首轮唤醒做任务理解与赋予，后续执行效果不好（停滞/失败/质变）时再次唤醒并重新调整；值守哨兵 SNT（纯代码）决定"什么时候叫醒指挥官"；操作员并行干活、成果实时落在作战地图（frontier.sqlite + hypothesis.sqlite + strategy.md + events.jsonl）；VRF 像质量工程师只管交付物质量，否决交给 CDR 这个项目经理拍板。智能体间不直接对话，一切经黑板交换。

## 流程各环节详解

### ① 接入（Gateway，不做规划决策）

- 唯一入口 `secpt solve <dir>`：入口 URL 抽取、附件自动解压、先验匹配（关键词→语义兜底）
- **入口不做任务理解**：题面原样交给指挥官首轮唤醒理解、分类、决定打法（benchmark 战役跑分也是 commander 模式矩阵的一条路径，不是入口闸口）
- 或从 Web 控制台发消息：**接战确认闸**——指挥官先产《接战单》（目标重述/成功标准/风险与约束），人工在作战地图确认后才编成派单；勾选"自动下发"可跳过确认（即发即打）。对话即任务流，对话中随时可发布任务（消息=人工直投意图入当前作战地图；任务收敛后发送自动续接编排，同一张地图）。对话面板按语义分层渲染：顶部时间线刻度轴按真实时间标出每个对话节点（用户提问/各轮响应/工具调用），hover 显示精确时间与耗时，点击定位到对应轮次或工具卡；每轮收进一个可折叠 ROUND 面板，头部显示轮次序号、实时状态（运行中=「进行中，已用时 X.Xs」，收口后=「已完成，用时 X.Xs」）与累计总耗时；面板内先列用户输入（人像图标），再把智能体的思考/规划/状态链收进「思考与工作」面板（灰蓝），把工具调用与执行结果收进「工具与结果」面板（琥珀，含命令+输出+Copy、写文件卡折叠显示并回源读全文）；全局配色收敛为 5 类（灰蓝=思考/用户、琥珀=工具、绿=成功、红=错误、浅灰=时间）。任务运行中输入框顶部显示三点生成指示，并出现红色 Interrupt Task 中断按钮；窄屏下导航与任务历史改为横滑条，舞台占满剩余高度

### ② 首轮唤醒（CDR，事件驱动无会话状态）

- 理解客户原始需求 → 产 GoalSpec（落盘 goal.json，resume 复用）
- 从角色目录选 profile（8 个，各带场景指引，落盘 role.json）+ 从经验目录选 priors + 从技能目录选 skills
- 建假设树 v0 + 战略文档 v0，拆分派首批任务；派单三要素：
  - `max_wall_s`——何时截止，到点即结算
  - `notes`——边界/风险/禁止动作
  - `self`——简单任务 CDR 当场自执行，不占操作员名额
- 派单可逐任务带 profile/prior/skills——同一 engagement 的不同操作员可领不同角色与技能包
- **不是只做一次**：后续执行效果不好（停滞/连续失败/方向性质变）时，CDR 再次被唤醒会重新调整角色/场景/经验/技能——换人、换打法、换经验都可能，调整结果按任务重新赋予

### ③ 稳态循环（占 90% 时间）

```
操作员 A/B/C…（main lane，任务池原子认领，通用 ReAct）
   │ Thought → Action → Observation，完成结算再领下一单
   │ 耗时粗活（下载/批量搜索/爆破/爬取）→ delegate_logistics 委托后勤 QM（后台 lane，不阻塞主路径）
   ▼
SNT 结算一切事件 → 作战地图（任务池/假设树/战略文档/事件流，Web 面板实时可见）
   │
   ├─ 无触发器 → 循环继续，CDR 不在场
   ├─ 地图质变（新 flag / confirmed 假设 / 新 findings）→ 当轮插队唤醒 CDR 读图重估（不等定时器）
   └─ 值守触发器（预算里程碑 30/50/75% / 停滞 / 巩固定时器 / convergence）
       → 多信号攒一轮一次唤醒全交付（列表化简报，防信号饿死）
       → CDR：信念结算 → 方向决策 → 必要时重选角色/场景/经验/技能
           → 派单 → 全量重写战略文档（防漂移闸：大转向必须留论证）→ 休眠
```

- 操作员目标编译注入：【当前主攻方向】（战略文档摘录）+【已验证漏洞·跨资产情报】+【技能包】+【历史经验提示】
- 卡壳时：SNT 唤醒教练 COH → 诊断 → 要求 CDR 重写战略文档 → COH 退出（只介入不抢决策权）

### ④ 收敛与交付（裁决链：SNT 递信号 → CDR 拍板 → VRF 质检）

- **收敛裁决归 CDR**：SNT 两处自然收敛只递 convergence 信号不拍板；CDR 读图裁决 deliver 或补产；连续两次不交付才回退兜底收口（不可悬挂）
- **交付核验归 VRF（质量工程师）**：证据链核对 + 独立复现（异构验证：不同工具路径再证一次）；只评质量不拍板
- **否决回 CDR（项目经理）**：quality 信号穿透交付等待送达 CDR 综合裁决（修复/坚持/转向）；复验任务入池推进尝试计数，耗尽仍未过 → confirmed 信念回滚（置信度×0.6 + [rollback] 证据），按未验证收口
- **多任务并行**：同一目标被并行 engagement 同时开打时，全局目标租约（`var/leases/`，TTL 自动过期，advisory）告警并建议限速避让，不硬阻断

### ⑤ 收口归档（可发布）

- SNT 写 retro.md 复盘 → 学习层蒸馏经验入 `var/knowledge/experiences.jsonl`（下次同类任务按命中率 Top-3 自动回灌简报，越用越好用）
- 产物自动压缩（`SECPT_COMPRESS=0` 关）：events.jsonl 超 500 行轮转归档（`archive/*.jsonl.gz` + watermark，seq 保单调，Web 事件流自动回读归档段）+ sqlite VACUUM + 大日志 gzip——**Web 是主要发布入口，job 目录可直接打包搬运**

## 快速开始

```bash
# 1. 准备环境
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export DEEPSEEK_API_KEY=<key>          # 或任意 OpenAI 兼容端点（见 docs/架构现状.md 模型路由）

# 2. 一键执行——任务类型与打法都不需要用户判断：
secpt solve challenges/rois-saml-sso
secpt solve <dir> --prior saml --wall 1800    # 覆盖项仍可干预
secpt solve <dir> -p multistage               # 多阶段：recon → getshell → privesc（折叠为意图链）

# 3. 无 key 干跑（FakeLLM 全链路）
SECPT_FAKE_LLM=1 secpt solve challenges/rois-saml-sso

# 4. Web 指挥台（对话发布任务 + 作战地图实时观测）
secpt web --port 8080

# 实时观察（另开终端）：逐步思考/工具调用/响应/发现/压缩
secpt watch <job_id>

# 5. 测试与规范
make check                             # ruff check + ruff format --check + pytest（提交前必跑）
make recheck REPORT=<report.json>      # 可选：复跑判据门禁
```

## 角色速查

| 角色 | 性质 | 在流程中的位置 | 一句话职责 |
|------|------|----------------|------------|
| **指挥官 CDR** | LLM，事件驱动 | ②首轮唤醒（任务理解+角色/场景/经验/技能赋予）、③信号唤醒（效果不好时重新调整赋予）、④收敛/质量裁决 | 读确定性简报 → 信念结算 → 方向决策 → 派单 → 全量重写战略文档 |
| **值守哨兵 SNT** | 纯代码零 LLM，常驻 | ③结算一切事件、递触发器；④递 convergence；⑤收口 | 结算入图 + 确定性触发 + 兜底收口 + retro.md |
| **操作员 OPT**（A/B/C…） | LLM，ReAct | ③main lane 并行领单 | 领单干活到完成；按派单角色/技能工作；粗活委托后勤 |
| **军需官 QM** | LLM，队列消费者 | ③logistics lane 后台 | 下载/批量搜索/爆破/爬取等耗时活，只回写不决策 |
| **核验官 VRF** | 确定性 + 按需 LLM | ④交付门 | 质量工程师：证据核验 + 独立复现，否决经 quality 信号报 CDR |
| **教练 COH** | 确定性，休眠唤醒 | ③卡壳介入 | 诊断 → 要求 CDR 重写战略文档 → 退出 |

> 旧五角色模板（recon/exploit/lateral/verify/report）保留为 legacy：`SECPT_ROLE_TEMPLATES=1` 恢复；编排模式统一由 `resolve_orch_mode()` 裁决（stages 清单 > 团队 legacy > 指挥官 > legacy），歧义组合运行时告警。

## 关键机制清单

| 环节 | 机制 | 说明 |
|------|------|------|
| 编排 | 唯一执行编排 = frontier 意图驱动 | stages 折叠为线性意图链；workers 由 profile YAML 数据驱动；多任务 `secpt submit -t spec.yaml -j 4` |
| 并发 | 任务池原子认领 + 双通道 | BEGIN IMMEDIATE 认领；main/logistics 双 lane；EventSink 线程锁 + flock + PEV_LOCK 父流串行，seq 跨进程单调 |
| 质量 | 交付门 + 信念回滚 | 复验异构验证；耗尽未过 → confirmed 置信度×0.6 + [rollback] 证据 |
| 记忆 | 作战地图 + 学习闭环 | frontier/hypothesis/strategy/events 四件套；经验蒸馏→命中率回灌；playbook 族级打法 + techniques YAML 数据驱动 |
| 技能 | ZCode 式动态发现 | `skills/<name>/SKILL.md` 即插即用；`SECPT_SKILLS_DIRS` 扩展（后写覆盖）；按任务挂载注入操作员目标 |
| 长任务 | 断点续跑 + 自愈 | compaction 非阻塞 + 进度锚点 .progress.md（5 步落盘）+ checkpoint resume；`--wall 0` 7×24 |
| 评测 | AI 评判 | `secpt judge <job_id> [--ground-truth FILE]`——反作弊 → Jev 门控 → LLM judge 终裁 |
| 观测 | Web 指挥台 + 事件流 | OpenAI chat 式对话面板（右侧用户/左侧按智能体分组）+ 作战地图面板；18 类事件 seq 游标增量；structlog JSON + Prometheus 指标 |

## 架构分层（代码驱动，读一遍就能跑）

```
┌─────────────────────────────────────────────────────────────────────┐
│ 路由层 solve.py / dag.py —— 统一执行路由                             │
│   benchmark profile → run_benchmark_campaign；其余 → run_frontier   │
└────────────┬───────────────────────────────┬───────────────────────┘
             │                               │
             ▼                               ▼
     run_frontier()              run_benchmark_campaign()
     (frontier.py · 唯一执行编排)  (benchmark.py · 平台协议层)
     ┌────────────────────┐      ┌──────────────────────────────┐
     │ 清单型：stages 种子 │      │ for round in 1..3:           │
     │  链（多阶段折叠）    │      │   run_benchmark()            │
     │ 探索型：CDR 派单    │      │     ├─ ChallengeLedger       │
     │  triage → main /    │      │     ├─ chunk_ranges          │
     │  logistics 双通道； │      │     └─ run_frontier          │
     │  _worker_loop ×N +  │      │ 心跳线程 + 平台探针          │
     │  后勤 worker；      │      └──────────────┬───────────────┘
     │  交付门；CDR 裁决收敛 │                     │
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
| **编排模式唯一裁决** | `resolve_orch_mode()` 归约全部编排开关为一个显式模式，frontier 入口打印模式与告警 | 消除散落 env 判断的组合爆炸；legacy 路径显式弃用告警 |

## 插件示例（集成 getshell 能力）

```yaml
# tools/plugins/getshell.yaml
name: getshell
description: 对目标执行 getshell 尝试
schema: {type: object, properties: {target: {type: string}}, required: [target]}
run: "getshell --target {target} --json"   # 换成你环境里真实的工具 CLI
timeout_s: 300
```

内置工具：`web_search`（Tavily/Brave/SearXNG）/ `browser_*`（Playwright）/ `http_request` / `session` / `oob`（外带回调）/ `traffic`（流量武器化：重放+单参数变异）/ `recon_parse` / `crypto_helpers` / `analyze_binary` / `sanitize_oracle`。子智能体 `SECPT_DELEGATE=1` 开（独立上下文、结论压缩回传、结构性禁递归）。

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
│   │   ├── orchestrator/      # ★编排层（唯一执行编排 = frontier 意图驱动）
│   │   │   ├── dag.py         # 统一路由：benchmark → campaign；其余 → frontier（augment_objective 拼接）
│   │   │   ├── frontier.py    # 意图调度（CDR 派单 / stages 折叠 / main+logistics 双通道
│   │   │   │                  #   + _worker_loop×N + 交付门 + 教练注入 + retro.md + 收尾压缩）
│   │   │   ├── hypothesis.py  # 假设树（CDR 世界模型：状态机+置信度+证据锚定）
│   │   │   ├── watch.py       # 值守层：失败类型化 + 确定性触发器（预算/停滞/内务）
│   │   │   ├── commander.py   # CDR：无状态唤醒会话 + 战略文档 + 防漂移闸 + 简报编译器
│   │   │   │                  #   （技能目录 / 历史经验命中率回灌）
│   │   │   ├── modes.py       # 编排模式唯一裁决 resolve_orch_mode（+ 歧义告警）
│   │   │   ├── global_lease.py # 全局目标租约（var/leases O_EXCL+TTL，advisory）
│   │   │   ├── triage.py      # JEV 风格快判：任务分级 / 风险 / main vs logistics / 结果快检
│   │   │   ├── coach.py       # 卡壳教练：重试/RESET 时给提示、缩任务、换路径
│   │   │   ├── delivery_gate.py # 交付门：证据核验、独立复验任务、结束判定
│   │   │   ├── benchmark.py   # 多轮 campaign（外层重试 + run_benchmark 内层跑 frontier）
│   │   │   ├── ledger.py      # ChallengeLedger 跨进程互斥账本（benchmark 防双开）
│   │   │   ├── roles.py       # legacy 角色定义 + role_stage（SECPT_ROLE_TEMPLATES=1 才启用）
│   │   │   ├── policy.py      # prove_goal 量化验收 + DEPRECATED_MODULES 红线
│   │   │   └── goals.py       # 目标拆解 + asset_scope 结构化（render_goal_system）
│   │   ├── profile/           # profile YAML loader + system_extra 渲染（benchmark/hunter/pentest/redteam…）
│   │   ├── context/           # S3 四件套：compaction(非阻塞) / rollover / budget / summarize
│   │   ├── sustain/           # 产物压缩：compress（events 轮转归档保 seq + VACUUM + 日志 gzip）
│   │   ├── skills.py          # 技能动态发现：discover_skills/valid_skill/load_skill（后写覆盖）
│   │   ├── memory/            # warroom 四表（历史/可选组件；协作走 intel 图 + findings + traces）
│   │   └── watchdog.py        # 空转监控（卡死切换器六判据 + 进度锚点）
│   ├── governance/            # scope / ratelimit / intercept / audit（审计 JSONL）
│   ├── evaluation/            # AI 评判层（anticheat→Jev→judge→verdict）
│   ├── findings/              # 漏洞发现全生命周期（验证纪律/CWE/CVE 去重）
│   ├── intel/                 # 图记忆（资产图+血缘图+覆盖度+冷压缩）
│   ├── traffic/               # harness + recorder + store + replay(武器化：重放+单参数变异)
│   ├── report/                # CWE 映射 + 报告交付（md/md-zip 证据包）
│   ├── learning/              # 经验闭环：distill / store / retrieve + playbook + techniques
│   ├── tools/                 # plugin_loader（plugins/ 外部能力集成）
│   ├── challenge/             # 挑战加载（题面/入口/附件，solve 的原材料）
│   ├── knowledge/             # priors/ 先验打法 + prior_map.yaml + techniques.yaml 通用打法
│   └── web/                   # 指挥台：server(stdlib HTTP) / api(payload+对话续接+归档回读) / static(对话即任务流 UI)
├── tools/plugins/             # 用户插件（YAML 声明式 / Python）
├── skills/                    # 技能目录（SKILL.md + frontmatter，按任务挂载给操作员）
├── docker/                    # agent.Dockerfile（Kali 基座比赛镜像）/ sandbox.Dockerfile / entrypoint.sh
├── scripts/                   # build-agent-image.sh / prepare-image-seed.py / recheck.py / archive_runtime.py
├── seed/knowledge/            # 构建时注入镜像的脱敏知识种子（experiences.jsonl + playbooks/*.md）
├── tests/unit/**              # FakeLLM 为地基，无 key 全链路可测
├── docs/                      # 架构现状(权威) + 系统速览(5分钟版) + 实验报告 + 各设计文档
├── logs/                      # 运行日志归档（gitignored）
└── var/                       # 运行时数据（jobs/knowledge/leases/audit…，gitignored）
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
