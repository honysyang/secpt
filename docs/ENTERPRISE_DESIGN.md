# secpt 企业级长任务高复杂场景 · 差距分析与演进设计

> 定位：面向**企业级**运营的安全智能体平台——支持**天级长任务**、**高复杂度场景**
> （多目标、多阶段、并行扇出、人机协同）。本文是差距分析（对标开源 harness）
> 与演进设计，是后续里程碑（M1+）的施工依据。
> 现状基线：v0.8（commit d2683ab，378 个测试）。文档记录设计；实现以代码为准。

---

## 0. 一句话差距

secpt 已完成「**单 mission 引擎**」：事件账本、动作网关、JEV 裁决、教练、
确定性规则、分析文档全部就绪。距离「**企业级平台**」差四层：
**执行架构**（串行→并发、host→沙箱）、**长任务运营**（进程级持久化、上下文压缩、
监督守护）、**人机与治理**（审批挂起、多 mission 治理面、集成生态）、
**度量闭环**（模型路由与成本归因、评测基线）。

---

## 1. 对标开源 harness：借鉴矩阵

| harness | 关键机制 | 对 secpt 的判定 |
| --- | --- | --- |
| **OpenHands**（前 OpenDevin） | EventStream 架构；Condenser 上下文压缩；per-mission Docker runtime；agent 委派 | EventStream ≈ 我们的事件账本（**已收编**且更强：幂等键+确定性投影）。**待收编**：Condenser（长任务上下文压缩）、容器 runtime（执行隔离）。委派已有（worker） |
| **SWE-agent** | Agent-Computer Interface：工具接口本身即护栏 | **已收编**且更严（网关 8 步门 + 封闭 blocked_reason 枚举）。补充：ACI 的「输出结构化」经验已用于 clue.meta |
| **Claude Code / ZCode 类 harness** | 子代理上下文隔离；hooks（PreToolUse/PostToolUse）；权限模式；**compaction/summarization**；background tasks + 任务面板 | 子代理隔离（**已有**）。**待收编**：compaction（长任务记忆压缩）、后台任务面板、用户自定义 hook 扩展点。权限模式 ≈ 教练三档（已有） |
| **LangGraph** | durable execution：每超级步 checkpoint；**interrupts**（人审挂起/恢复）；time-travel | checkpoint（**已有**，快照+重放）；time-travel ≈ 账本重放（**已有**）。**待收编**：interrupt 语义——`needs_approval` 从「拦截」升级为「挂起-裁决-恢复」 |
| **Temporal** | workflow-as-code 持久执行：重试策略、**heartbeat**、timers、cron、watchdog | 心跳/租约（**已有**）。**待收编**：mission supervisor daemon（未完成 mission 自动恢复、cron 攻击窗口、SLA 告警）——这是「天级长任务」的运营底座 |
| **PentAGI** | per-mission Docker 隔离；auditor 代理；向量+图记忆；报告 | auditor ≈ JEV + 二验信号（**已有**）。容器隔离（**待收编**，同 OpenHands）。图记忆**不收编**可变存储（撞账本红线）——「findings 关联图」作为账本投影可收编 |
| **CAI** | 按 task 的**模型路由**（难度×成本）；工具注册表；benchmark 纪律 | **待收编**：模型路由与每任务成本归因——当前单客户端（`load_client()` 一个实例），budget.charge 只按超级步不按任务 |
| **Aider** | repo map（tree-sitter 代码地图） | 线索索引（E1）**已收编**同类思想；code 类任务可补 tree-sitter 式结构地图（低优先） |
| **Letta（MemGPT）** | 自编辑分层记忆 | 分层记忆 L1–L4（**已有**）+ 只读继承纪律。可变自编辑**不收编**（可变真相源） |
| **E2B / microVM 生态** | 每次执行一次性沙箱 | 执行隔离的实现选型之一（**待收编**，与 Docker runtime 二选一或并存） |
| **ARTEX / sec-agent**（`docs/WORKFLOW.md` §8–9） | 传令官/intercept/黑板；会话级编排 | 结论已固化：审批闸与策略编译闸**未实现**（见 §2.4）；其余已收编或已否决 |

**总原则**（沿用 `docs/WORKFLOW.md` 纪律）：抄机制不抄存储——任何借鉴不得引入
可变真相源；编排决策可以给模型，放行必须给代码。

---

## 2. 差距清单（现状证据 → 差距 → 影响）

### 2.1 执行架构：串行 + 宿主机直执行（最大差距）

**现状**：`pa/runtime/scheduler.py` `_dispatch` 在 `for task in batch:` 中**同步串行**
执行 worker；`max_workers` 实际是「每轮批大小」而非并发度。工具经
`pa/infra/executor.py` 在**宿主机**直接起子进程（shell 白名单 nmap/curl/...），
workspace 是 mission 共享目录。`ARCHITECTURE.md §11` 风险 1「同步执行导致墙钟
被最长任务拖住」原样存在。

**差距**：① 无真并发——8 个 probe 类任务本可 8 倍提速，现在逐个跑；
② 无隔离——工具进程与引擎同权限同网络，一次工具失控（如 nmap 脚本引擎）
即危及宿主；③ 共享 workspace 有写冲突隐患（并发后必然踩）。

**影响**：长任务墙钟不可接受；企业环境无法通过安全评审（工具在分析师主机上跑）。

### 2.2 长任务运营：引擎能恢复，但没有人守着它恢复

**现状**：单进程 `run()` 前台执行；`resume` 需要人手动调用；租约过期回收、
heartbeat、`SECPT_DIE_AT_STEP` 断电演练都在；`deadline` 字段存在但无 SLA 告警；
无 cron/窗口控制（`docs/CONTROL_PLANE.md` 曾设计「定时任务：cron 控制攻击窗口」）。

**差距**：① 无 supervisor daemon——进程死了 mission 就躺着，靠人巡检 `status`；
② 无攻击时间窗（企业场景常要求 22:00–06:00）；③ 无 watchdog/告警
（mission 卡死、模型持续失败无人知）；④ 分析文档（v0.8）只有收口后产出，
长任务运行中无中途快照式分析。

### 2.3 上下文工程：预算裁缝有了，压缩没有

**现状**：E4 `_fit` 按「线索→经验→任务细节」裁剪 + 截断提示；L2 线索索引、
L3 轮末简报（rolling brief，2000 字 schema 闸）、untrusted 环形缓冲（3×2KB）。
真跑数据：prompt 斜率 59–615 字符/步，几十步内可控。

**差距**：① **天级任务**（数百步）下线索冷区只靠「另有 N 条超预算」一句话丢弃，
无分层压缩（OpenHands Condenser：把老旧证据折叠成摘要事件，原文仍可在账本回查）；
② 简报只覆盖「上一轮」，无「截至目前的战役级摘要」；③ worker 换手（重派）
时只有 tried 指纹 + last_failure，无「本次尝试前的战场快照」。

### 2.4 人机协同：审批是拦截，不是挂起

**现状**：网关 `needs_approval` 直接 block（任务转 blocked）；确认只在 `do`
立项时（验收合同面板）；无运行中人工介入通道（`human.directive` 事件类型存在
但无生产者）；无 Web/IM 控制台（ARTEX 时代的 web 已随重写移除）。

**差距**：① escalation rail 未实现（`docs/WORKFLOW.md` §3 设计了
`PermitResult: allow|block|escalate` 与幂等裁决键，代码里只有前两个值）；
② 长任务运行中不能暂停/追加指令/代改验收；③ 企业流程要求审批留痕与
审批人身份，现在没有签发人字段（`verdict_by` 有、`signed_by` 无）。

### 2.5 模型与成本：单客户端、步级计费

**现状**：`load_client()` 全局单实例（`SECPT_MODEL` 一个主模型 + 一个 fallback）；
`budget.charge` 按超级步结算（真实 usage），无法回答「t-0007 这个失败任务烧了
多少 token」；`task.budget.tokens/risk` 字段存在但**没有执行点**（只有 steps
限制 worker 循环）。

**差距**：① 无按任务的路由（简单指纹匹配用便宜模型、复杂审计用强模型——
CAI 的核心做法）；② 无任务级成本归因与任务级预算强制；③ 企业需要
成本报表与配额硬顶（超了就停，不是记账）。

### 2.6 度量闭环：评测基线缺失（自证不了价值）

**现状**：`stats` 给过程指标（vs_rate、解析失败率、confirmed/100k）；分析文档
给单次运行建议；**没有** ground truth 靶集、没有 plain-agent 对照组、没有回归
重放比对。`docs/CONTROL_PLANE.md §14` 设计了完整评测体系（Vulhub 10 + HTB 5 +
自建 5），代码为零。

**差距**：无法证明「这套 harness 比裸 ReAct 强」——企业采购与自我迭代都
需要这个数。每次真实运行的 analysis.md 在攒素材，但没有靶场把它变成结论。

### 2.7 治理与多租户：单 mission 视角

**现状**：一次只跑一个 mission（前台进程）；经验库工作区级共享（无租户隔离，
README 已知限制明示）；无全局配额、无优先级抢占、无 mission 依赖
（如「先 web 后内网」的 campaign 编排）。

**差距**：企业场景是多 campaign 并行：不同客户/不同授权、资源配额、
成本上限、结果汇总。当前 `missions/` 目录自治，无治理面。

### 2.8 交付与集成：报告有了，交付链没有

**现状**：report.md（验收矩阵/资产/findings）+ analysis.md（运行分析）+
evidence 内容寻址库；`ARCHITECTURE.md` 规划的 `report/bundle.py`（交付物打包）
**从未实现**；无 HTML/PDF、无签名（`signed_by/signed_at` 数据结构预留未用）、
无工单/SIEM/IM webhook 集成。

### 2.9 知识闭环：经验会积累，质量无门控

**现状**：experiences.jsonl 收口提炼（origin/finish_reason 溯源）；注入按
type 过滤。**差距**：经验质量无门控（DESIGN.md §2.5 要求的「证据校验+人审」
门控未实现——一次失败的 mission 可能污染后续注入）；无检索效果度量
（注入的经验是否改变了任务成败？无 A/B）。

---

## 3. 演进后目标架构

```
┌─────────────────────────── 治理面（新） ───────────────────────────┐
│ Portfolio Manager：campaign 队列 · 配额/成本硬顶 · 优先级 · 租户   │
│ Approval Console（Web/IM）：审批卡 · 中途指令 · 报告签发           │
└──────────────┬────────────────────────────────────────────────────┘
               │ mission.spec ↓        ↑ analysis/report/events
┌──────────────┴────────── Supervisor（新，daemon） ─────────────────┐
│ watchdog（卡死/失败告警）· cron 时间窗 · 自动 resume · SLA         │
└──────────────┬────────────────────────────────────────────────────┘
┌──────────────┴──────────── 引擎（已有，增强） ─────────────────────┐
│ 并发派发池（M1）· 上下文 Compactor（M3）· Approval Rail（M2）      │
│ Model Router + 任务级成本记账（M4）· 账本/网关/JEV/教练（不动）    │
└──────────────┬────────────────────────────────────────────────────┘
┌──────────────┴──────────── Sandbox Runner（新） ───────────────────┐
│ per-mission 容器（网络策略/资源限额/只读根 + workspace 挂载）      │
│ 降级路径：host 执行（现状，lab 模式保留）                          │
└────────────────────────────────────────────────────────────────────┘
```

原则不变：账本唯一真相、网关唯一副作用出口、JEV 唯一结论出口——
新组件全部挂在账本事件上，不引入第二个真相源。

---

## 4. 关键设计

### 4.1 M1 并发派发池 + 执行沙箱

**并发**：`scheduler._dispatch` 改为 `ThreadPoolExecutor(max_workers)`；
worker 本身是 LLM IO 密集（工具子进程是短促 CPU），线程池足够（GIL 影响
有限且与 ARCHITECTURE「M3 换进程池」相比先易后难）。必须先解决的三个
并发正确性问题：

1. **StoreWriter 线程安全**：`append`（读改 `_seq`、append+fsync）与
   `_index` 需进程内锁（`threading.Lock`），事件顺序 = 锁内序——账本
   仍全序，投影不变。
2. **workspace 隔离**：每任务 `workspace/_tasks/<task_id>/` 作为其
   ToolContext.workspace（fs/shell workdir、附件只读挂 _attachments）；
   网关传参已有 ctx，改动局限 `tools/base.ToolContext`。
3. **线索/判定时序**：worker 并发后 judge/clue 的 `state` 读写需要
   每任务快照或在锁内 apply——设计为「worker 返回后在主线程 merge」
   （现状 merge 已在 dispatch 后），worker 内只读 state 快照 + 写事件，
   与「单写者」纪律一致。

**沙箱**：`pa/infra/runner.py` 新增 Runner 抽象（`host` / `docker` 两实现，
`SECPT_SANDBOX=docker|host`）：容器以 mission 为单位启动（一次/复用），
workspace volume 挂载、网络策略（默认仅允许 allowed_scope 出站，经容器
网络策略或代理强制——呼应 DESIGN.md「网关负责意图，网络策略才是边界」）、
CPU/内存限额、非 root 运行。工具白名单在容器内仍有意义（意图校验+留痕）。

**验收**：8 个 probe 任务并发执行墙钟 ≤ 串行 1/4；并发下 `kill -9` 恢复后
事件账本全序一致；docker 模式下宿主机无工具进程；既有 378 测试不改语义
通过（host 模式）。

### 4.2 M2 审批 rail + 中途人机协同

落地 `docs/WORKFLOW.md §3` 已设计的语义：

- 网关第 4 步 `needs_approval` 从 block 改为 **挂起**：写
  `approval.requested` 事件（action 全量参数 + 幂等键），worker 该动作
  等待（带租约），任务转 `blocked(needs_approval)`；
- 裁决入口：`secpt approve <dir> <idem> --allow|--deny --by <人>`
  （CLI 先行，Web 后接）；`allow` → 任务回 ready 重放该动作；
  `deny` → 永久 blocked；裁决幂等键 = 动作 fp；
- 中途指令：`secpt steer <dir> '<自然语言>'` → `human.directive` 事件
  （信号级唤醒已有，orchestrator 上下文注入 directive 段）；
- 报告签发：`secpt sign <dir> --by` → 报告尾部落 `signed_by/signed_at/
  报告 sha256`（HMAC 本地签名，密钥走 SECPT_SIGN_KEY）。

**验收**：production 环境下 critical 工具触发挂起→CLI 批准→任务完成
全链；steer 指令在下一轮编排上下文可见并有 decision.made 响应；报告
签名可独立校验。

### 4.3 M3 长任务运营（Supervisor + Compactor）

**Supervisor daemon**（`secpt supervisor`）：周期扫描 `missions/`——
发现未终态 mission → 检查租约/心跳 → 自动 `resume`（幂等）；对
`failed(Model)` 连续 N 轮的 mission 发告警（webhook/IM 集成点）；
cron 时间窗（`mission.json` 增 `window: "22:00-06:00"`，窗内才派发，
窗外只心跳）。**验收**：kill 引擎进程后 supervisor 在一个周期内自动
恢复 mission 并收口一致。

**Compactor**（对标 OpenHands Condenser，确定性优先）：新增
`memory.compact` 事件——当 `state.clues` 超阈值（如 >200 条）时，把
最老的 N 条折叠为 `clue_digest` 线索（按 kind 聚合：kind/数量/代表键/
来源任务），原文仅在账本（trace 可查），投影表替换。轮次摘要升级：
`memory.reflect` 已有 rolling brief，增加「战役级」累积摘要（每 10 轮
强制一次，带验收进度）。**验收**：500 步模拟任务 prompt 斜率不发散
（≤200 字符/步），折叠线索可从账本重放完整还原。

### 4.4 M4 模型路由 + 任务级成本归因

- `pa/infra/router.py`：`RouterClient(LLMClient)` 按 `(phase, task.type,
  task.budget.risk, 剩余预算)` 选模型（`SECPT_MODEL_POOL` JSON：多档
  模型+成本系数）；对 `worker` 简单任务（recon/probe）用低档，
  `code` 审计与 orchestrator 用高档；
- 计费下沉：`llm.calls` 已带 usage，charge 时按 `task_id` 分桶（worker
  相调用都带 task 上下文）→ `task.spent_tokens` 投影；任务级预算强制：
  worker 循环内超 `task.budget.tokens` → `budget_blocked` partial
  （与 budget_exceeded 同路径）；
- 成本报表：`analysis.md` 增「按任务成本」段；`stats` 增每模型成本列。

**验收**：混合路由下 confirmed/100k 成本下降可测（待 M5 靶集）；任务
超预算必然提前收口（有测试）。

### 4.5 M5 评测基线（自证价值的前置）

- `bench/` 目录：靶集 spec（docker-compose，Vulhub 子集 10 个 +
  自建 3 个），每个靶 ground truth（位置/可利用性/POC/判定 evidence）；
- 对照组：同模型 plain ReAct 单 agent（复用引擎关掉 planner/JEV/教练
  的「裸跑模式」，`SECPT_BARE=1`）；
- 指标：confirmed@预算、误报率、首次有效线索步数、成本/漏洞、人工介入次数；
- 回归：录制备份账本重放比对（账本确定性保证）。

**验收**：`secpt bench run --suite vulhub-lite` 产出对比报告；引擎改动
前后跑同一靶集，指标可复现（±10%）。

### 4.6 M6 治理面 + 交付链

- Portfolio：`missions/portfolio.json`（campaign → mission 列表 + 配额 +
  优先级），supervisor 按配额调度；全局成本硬顶；
- 交付链：`pa/report/bundle.py`——`secpt bundle <dir>` 打包
  report.md + analysis.md + evidence/（内容寻址）+ 账本 + manifest.sha256，
  可选 HTML 渲染；报告签名（4.2）。
- 集成点：webhook 事件出口（mission.finished/finding.adjudicated →
  IM/工单），只发事件不收指令（控制面仍走审批 rail）。

**验收**：bundle 在新机器上可独立校验全部证据 sha；webhook 断网不阻塞
收口（降级告警）。

---

## 5. 里程碑与依赖

| 里程碑 | 范围 | 验收 | 依赖 | 估计 |
| --- | --- | --- | --- | --- |
| **M1 并发+沙箱** | 线程池派发、StoreWriter 锁、per-task workspace、Docker Runner | 8 任务 ≤1/4 墙钟；恢复一致；容器无宿主进程 | 无 | 1–2 周 |
| **M2 审批+人机** | escalation rail、approve/steer CLI、报告签名 | 挂起→批准全链；steer 进上下文 | 无（可与 M1 并行） | 1 周 |
| **M3 长任务运营** | supervisor daemon、cron 窗口、Compactor、战役级摘要 | 自动恢复；500 步斜率不发散 | M1（恢复含并发） | 1–2 周 |
| **M4 路由+成本** | RouterClient、任务级计费与预算强制、成本报表 | 超预算必停；成本可归因 | 无 | 1 周 |
| **M5 评测基线** | 靶集、bare 模式、对比报告、回归重放 | confirmed@预算可复现 | M1（并发压测） | 1–2 周 |
| **M6 治理+交付** | Portfolio、bundle+签名、webhook | bundle 独立校验；断网降级 | M2/M3 | 1–2 周 |

顺序理由：先让「跑得快且隔离」（M1，企业安全评审的硬门槛），再补
「人能管」（M2，企业流程硬门槛），然后「跑得久」（M3）、「跑得省」
（M4）、「跑得有据」（M5），最后「管得多、交得出」（M6）。
M1/M2/M4 相互独立可并行。

---

## 6. 红线（演进中不改）

1. **账本唯一真相**：新组件（supervisor/router/compactor/portfolio）
   全部经事件通信，不建第二个可变状态源；压缩折叠原文必在账本可回放。
2. **网关唯一副作用出口**：沙箱不绕过 8 步门；MCP/新工具一律过门。
3. **判断给模型，放行给代码**：路由策略可以是配置，越界/审批判定必须是
   确定性代码。
4. **不自动利用**：工具面保持只读；利用验证走人工（payload 不进引擎）。
5. **不做多租户 SaaS 形态**（CONTROL_PLANE 既定）：治理面是单运营者
   多 campaign，不是公有云多租户。

---

## 7. 与既有路线图的关系

已完成（v0.4–v0.8）：流程叙事/分析文档、secrules/filerules 确定性规则、
finding 管道（稳定身份/二验/unlock）、工作流修复（batch 全量、合同门、
依赖失败传播、do 新开 run、进展判定）、status/log 命令完备、目录规范化。

本文 M1–M6 与 `docs/WORKFLOW.md §7` 的关系：M1–M3/E1–E4 与质量项已全部
落地；其「已确认的开工候选」中审批 rail 与策略编译闸归入本文 M2；
策略编译闸（policies.jsonl 立法半边）排在 M6 之后——先让审批跑通，
再谈把人类意图编译成策略。
