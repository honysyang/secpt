# secpt 工作流与作战室 · 设计决议

> 状态：与作者讨论达成共识（2026-10）；路线图全量收编完成——M1/M2/M3/M4/M5、E1/E2/E3/E4 与质量项均已实施（详见 §7 路线图状态列）。
> 配套：`BUILD_SPEC.md`（v0 施工单）、`ARCHITECTURE.md`（分层边界）、`CONTROL_PLANE.md`（判定矩阵）。
> 本文记录 v0 验收后新达成的五组决议：工作流推进方式、双层控制环、网关三态与策略编译闸、作战室（黑板）与信号级唤醒、四层记忆；文末附 ARTEX 对标结论（§8）与 sec-agent 对标结论（§9）。

---

## 0. 基线：真跑验证结论

`/tmp/secpt-verify-lake`（目标 lake.aimohu.com）全链路真跑已验证：

- 3 轮编排全部 `mode=orchestrator`，绑定 E2 修复后的 `RoundDirective` 契约，零降级；
- 机械门禁 `restart.decided` 自动复活 Model 失败的 t-0004，恢复链路按设计工作；
- `finding.adjudicated` 两杆（`/.git/` 高危，all suspected via jev）、coach I2 shadow 记 advisory、plateau(thrashing) 优雅收束；
- context.assembled 序列为 planner → orchestrator → worker×n；约 22 次。

暴露的质量项（待修）：① 同一 `/.git/` 重复 finding 未合并（F-999a640ed00b / F-c65732322ca8）；② probe 对 closed 端口每次等 2 分钟超时，触发 thrash（probe 超时需调短）；③ acc-6 判据写错（期望 `http.error`，实际证据 key 是 `tls.cert`）。

---

## 1. 工作流三原则（对标渗透方法论）

渗透更像一个**阶段推进的工作流**（侦察 → 枚举 → 探测 → 利用 → 报告），而不是自由探索。落到 secpt 上有三条约束：

1. **单一大脑**：主智能体是全 mission 唯一规划点（orchestrator），子智能体只是「带角色 prompt 的手」。不做点对点 agent 网络，不搞多主智能体协商。
2. **指令往下走，证据往上回流**：谁执行、执行什么定义下行方向；谁成功、为什么失败全部回到 store 汇聚，由主智能体在轮末读视图。通信不走进程间直连，一切经账本/白板。
3. **两道护栏不变**：智能体出口 `_guard_run` / `_guard_create`（CREATE_CAP、去重、DAG 校验），工具出口网关 8 步门。任何新能力（角色、批量、MCP）都必须从这两道护栏里过。

业界参照（PentestGPT / HackingBuddyGPT）都是 playbook 引导型，而非结构性阶段门——secpt 走 playbook 注入路线（M4），不做硬编码阶段状态机。

### 1.1 现有实现与设想的映射

| 用户设想 | secpt 现状 |
|---|---|
| 主智能体规划→执行→重规划 | `planner.orchestrate()`（已实施） |
| 子任务分给其他智能体并行执行 | scheduler 派发 worker（已有） |
| 教练员指导 | `pa/policy` coach 三档 off/shadow/enforce（已有） |
| 角色分配、skills 注入 | skills 注入已有，**角色分配未做（M1）** |
| 工具批量执行 | **未做（M2）** |
| 网关策略介入 | 占位已有（见 §3） |
| 阶段推进纪律 / 出口判据 | **未做（M1+M4 playbook / M3 出口审计）** |
| 横向上下文共享 | 简报机制不完整（见 §6 / E2） |

三个概念偏差已剔除：网关是唯一出口（8 步门就是闸口，无需再造）；不存在 agent 间直连（全走 store）；教练员不进决策路径（只发 advisory/约束，决策权在编排者）。

---

## 2. 双层控制环

```
慢环（主智能体，每轮）    Plan-Execute-Replan：机械门禁 → orchestrate → 派发 → derive_stats
快环（子智能体，任务内）   ReAct 带刹车：max_react_steps + 无进展退出（coach I2 接入）+ 收束转 blocked 交机械门禁
```

- 子智能体快环的「观察 O」一律经 evidence store 读取，**不读原始 stdout**——保证可观测、可追溯、防注入。
- 任何重规划仍由主智能体在轮末经 `_guard_*` 完成；子智能体发现问题只能转 blocked / needs_help，不能擅自改计划。
- 理由：LLM 子智能体并发下的事件驱动中断会引入对主智能体的竞争写与重入；心跳汇聚让重规划永远串行、幂等。

---

## 3. 网关演进：单骨架、多闸口、三态输出

网关是**策略控制平面的落位**（现在只是占位），不新增骨架，只增加闸口与三态输出：

```python
class PermitResult: allow | block(reason) | escalate(who)
```

| 闸口（8 步门内） | 三态语义 |
|---|---|
| 范围 / 工具白名单 | allow / block / escalate（超范围动目标可交人审批） |
| 风险档（critical × production） | 现判 block，将来 escalate 给人工 |
| 频控 / 期间限制（新增占位） | 同上 |
| 审批闸（escalation rail） | 挂起 **action**（非任务）：pending_approval + `approval.requested` 事件 + 裁决幂等键；approve → resume，reject → blocked |

- `escalate` 暂兼容为 block：**行为零变化**，纯类型占位，先落类型再落闸口。
- 与 `pa/policy` coach 是同一机制：coach 三档（off/shadow/enforce + I4 规则）就是策略层前身；不做两套策略系统。

### 3.1 策略编译闸：人不下命令，人立规矩（收编 ARTEX MainAgent 职能）

8 步门是「执法半边」——机械执行已有策略；还缺「立法半边」——把人类意图编译成策略。对标 ARTEX 后明确：MainAgent 不是网关，它是**人机接口的传令官**，把人类意图翻译成图写入（add_hint / set_goals / steer_work）。secpt 把这个职能收编进网关：

```
human.directive（自然语言意图）
  → LLM 编译为结构化策略条款（白名单 / 风险档 / 频控 / 范围）
  → policies.jsonl（数据驱动，append-only）
  → 8 步门机械执行（deny 消息必须引用策略 id）
```

三条纪律：

1. **条款是数据**：策略一旦编译落盘就是普通数据，任何轮次回放都能复现当时的门禁行为；改策略 = 追加新条款，不改写历史（与账本红线同构）。
2. **编译幂等**：同一 directive 携 idem key 重复编译只落一次盘（与提炼幂等同构）。
3. **编译失败 escalate，绝不静默放宽**：LLM 编译不出结构化条款时，宁可把 directive 挂起交人确认，也不放任「无策略放行」——失败的代价是等待，不是失控。

一句话分工：**审批闸管「这一件事能不能做」，策略编译闸管「这一类事以后怎么管」**。前者是逐案裁决，后者是一劳永逸的立法；两者共用 escalate/幂等/降级三套既有机制，不新增骨架。

---

## 4. 作战室 = 黑板（blackboard）

作战室不是新模块，就是 store 的组织方式：**没有消息推送，一切唤醒的实质都是「下一次心跳到来时，看白板上有没有该自己做的事」**。

### 4.1 主智能体何时唤醒（信号级唤醒 v2）

主智能体不是「无差别心跳死循环」：白板写入分两级，**只有信号级写入才置 dirty 位唤醒编排者**，噪音级只入账、留给下一轮醒来一次性消化。

| 级别 | 事件类型 | 行为 |
|---|---|---|
| 噪音级 | evidence / clue.added / audit / action.started / action.finished 等 | 只入账，**不唤醒**——轮末汇聚统一消化 |
| 信号级 | task.state_changed / approval.requested·adjudicated / human.directive / escalate / coach 升档 / plateau | 置 dirty 位，内核唤醒编排者 |

三条纪律：

1. **同轮合并**：同一轮多条信号只醒一次，编排者醒来一次性消化全部——信号唤醒的是「白板该看了」，不是「逐条处理队列」。
2. **finding 类置顶**：finding.proposed / adjudicated 是最高优先信号，醒后先看台账再排活。
3. **兜底闹钟保留**：固定心跳仍做停滞检查（plateau / 预算核对 / 死任务复活），信号唤醒**不替代**兜底——防「没有信号但该干活/收工」的静默停滞。

唤醒点清单（3+1）：

| # | 唤醒点 | 性质 | 现状 |
|---|---|---|---|
| ① | 开战 `mission.start` | 一次性 | 已有 |
| ② | **信号级写入**（见上表） | 常态唤醒源：信号 → dirty 位 → 编排者一次消化 | **已实施**（落点：`pa/runtime/wakeup.py` 分类表 SIGNAL_TYPES/FINDING_SIGNALS + mission 根 `wakeup.json` 持久化水位 + loop 判 dirty；finding 类置顶 → `round(wake=…)` orchestrate 前预消化；无信号无 ready → `scheduler.heartbeat` 机械兜底，含重排队/提升防死锁） |
| ③ | 裁决写回 `approve/resume` | 审批裁决本身就是信号级写入 | 随 escalation rail 新增 |
| ①' | 用户插话 `human.directive` | 信号级，最高优先 | 已有接口，语义待强化 |

关键立场：**子智能体收工（evidence/audit 类噪音）不唤醒主智能体**——只有「白板状态变了」（任务迁移、裁决、插话、escalate）才醒。对标已验证：ARTEX 的 TriggerEvent debounce 合并 + 持久化水印正是这一模型的工程样板（见 §8.3）。

优点对照：相比「每轮必醒」，信号级唤醒消掉了空转轮的心跳预算浪费；相比「每事件即醒」，同轮合并避免了并发子智能体风暴下对编排者的惊群唤醒——两者正好是用户主张「有新信息或状态写入就唤醒」的工程化收窄。

### 4.2 子智能体何时唤醒（只有两种，全被动）

④ **被派发** `dispatch`；⑤ **被复活** `restart.decided`。无常驻子智能体、不订阅任何事件、干完即走、退出即焚上下文。真跑已验证：t-0004 两次 Model 失败被机械门禁自动复活。

### 4.3 作战室记录什么（四件套）

| 件 | 是什么 | 性质 |
|---|---|---|
| 账本 | `events.jsonl` | 唯一可写真相，append-only |
| 白板 | 任务状态表（谁在跑 / blocked / 失败） | 派生视图，可回放重建 |
| 简报 | 轮末滚动 mission 记忆（L3，见 §5） | 派生产物，orchestrator 读简报不读账本 |
| 台账 | findings（从证据推导） | 派生视图，不手写结论 |

### 4.4 怎么记录（三条硬规则）

1. **只此一扇门**：全系统写作战室都走同一条 `store.append(结构化事件)`；写什么由事件 schema 决定，不由写者决定。
2. **白板可回放**：账本在，白板/简报/台账都能从回放重建；kill -9 恢复无感（`apply_event` 纯函数，恢复与正常运行同一条代码路径）。
3. **幂等键防重账**：提炼、收工、裁决类事件带 round/job 级 idem key，重复触发只入账一次。

---

## 5. 事件与记忆：账本是真相，记忆是视图

**原则：events.jsonl 永远 append-only 不可变；记忆全部是「从事件派生的分层视图 + 提炼产物」。改写记忆永远不改写账本；记忆丢了可重建，账本丢了才是灾难。**

### 5.1 四层记忆

| 层 | 谁在用 | 内容 | 生命周期 | 现状 |
|---|---|---|---|---|
| L1 工作记忆 | 子智能体 ReAct 单步 | 当步本任务 evidence 窗口 | 任务级，不落盘 | 已有（A3 窗口） |
| L2 任务记忆 | 子智能体 / 派发时 | clue 聚合视图（kind/key → 证据索引） | 任务级 | 已有，待巩固按类型过滤 |
| L3 使命记忆 | orchestrator | 轮末滚动简报 + 任务状态表 + findings 台账 | mission 级 | 有雏形，待接入 orchestrator 上下文（E2） |
| L4 长期经验库 | 新 mission 启动时 | 跨 mission 经验条目（任务类型过滤，精确匹配，不做向量检索） | 跨 mission | **全新**，与 M4 playbook 共用注入机制 |

### 5.2 四条红线

1. **记忆不改写账本**：任何组件不允许写 events.jsonl 之外的最终态；findings、状态表都从事件推导，restart/复盘永远一致。
2. **提炼幂等**：round 级 idem key 去重，同一轮重复提炼只写一次。
3. **LLM 提炼失败静默降级**：记告警、不阻塞 mission 收工（learning/evaluation 属可降级功能）。
4. **检索必须过滤**：L2 按 goal、L4 按 target 类型/领域过滤——防上下文爆炸；不按类型过滤等于把记忆撑爆成账本全文。

---

## 6. 工作流三大缺口

即本轮讨论真正的靶点：

1. **创建时阶段纪律**：bootstrap 6 任务一杆铺开无序 → 由 M1 角色分配 + M4 playbook 注入解决；
2. **阶段出口检查点**：thrash 无出口 → 出口审计 M3（evidence 完整性校验进入交付门），杜绝「看不出进展却不说」；
3. **横向上下文**：同暴露面重复报 finding → 由简报机制补全（E2）+ finding 合并（质量项）。

---

## 7. 路线图

### 工作流侧 M1–M5

| 步 | 内容 | 状态 |
|---|---|---|
| M1 | 角色分配：`TaskDraft.role` + 角色库（数据驱动）+ 角色 prompt/skills 注入 | **已实施**（角色库 roles/ + context 注入 + compile_briefing） |
| M2 | 批量执行：task 多 action，网关逐项过门 | **已实施**（models.CallBatch 决策 + worker.run_action 单步管路复用，逐项过 8 步门；`_MAX_BATCH=8` 失控闸；空 batch 静默回反思，兼容旧单动作行为） |
| M3 | 出口审计：evidence 完整性校验进入交付门 | **已实施**（pa/core/audit.py + loop._finish 挂钩 + 报告审计行） |
| M4 | playbook 注入（渗透方法论阶段引导） | **已实施**（playbooks/ 数据驱动，按任务类型注入） |
| M5 | MCP 工具接入：必须过 8 步门 | **已实施**（每个 MCP server 声明为独立 ToolSpec `mcp_<name>` 进 REGISTRY，每个调用都过 gateway 8 步门零旁路；`SECPT_MCP_JSON` 数据驱动声明（内联 JSON 或文件路径，缺席 = 能力缺席，坏条目逐条跳过降级）；stdio JSON-RPC 一次性会话 executor（initialize→tools/call，argv 列表 spawn、返回线程 + deadline 扫描、killpg 收割），失败一律折成非零 exit 的 ProcResult） |

### 记忆侧 E1–E4

| 步 | 内容 | 状态 |
|---|---|---|
| E1 | store 查询视图巩固：L2 统一经 store 读、按类型过滤 | **已实施**（store/view.py filter/clue_summary + context L2 索引） |
| E2 | reflect 简报接入 orchestrator 上下文 | **已实施**（memory.reflect_round + orchestrator round_brief 段 + 冷区 digest） |
| E3 | 长期经验库 `experiences.jsonl`（跨 mission 提炼与注入） | **已实施**（pa/core/experience.py：SECPT_EXPERIENCES 覆盖 + 工作区级缺省，origin 幂等折射，loop._finish 收口钩子，planner/orchestrator 全库 + worker 按类型过滤） |
| E4 | context token 预算裁缝 | **已实施**（context._fit 裁剪 + `_TRIM_HINTS` 指引：被裁段尾提示去哪查；`SECPT_MAX_CTX_CHARS` env 覆盖预算，非法/非正值回缺省 120_000） |

### 质量项（随做）——已全部实施

- ~~probe 超时调短（closed 端口 2 分钟 → 秒级，SECPT_PROBE_TIMEOUT）~~ 已实施，消 thrash；
- ~~重复 finding 合并（同 asset+fingerprint 降杆合并台账；合并语义入 derive 投影，见 §9.2-3）~~ 已实施；
- ~~真跑结束后自动落盘 `report.md`~~ 已实施（loop.py 终态单点挂钩，三收口路径全覆盖）；
- ~~acc-6 判据修正（判据 evidence 类型与实际产出的证据 key 对齐）~~ 已实施。

### 已确认的开工候选（ARTEX 对标后排序）

1. **② scheduler 改造**：信号级唤醒（噪音/信号分级置 dirty 位 + 同轮合并 + 兜底闹钟）+ `_dispatch` 串行→并发派发——小而独立，直指当前最大弱项；
2. **① 网关 v2**：`PermitResult` 三态 + escalation rail + 策略编译闸（policies.jsonl）；
3. **E1+E2**：记忆视图巩固 + 简报接入（冷区 digest 样板见 §8.3，索引/检索引导收编见 §9.2-1，连带修质量项）；
4. **M1 角色分配**（可合并 M4 playbook 注入）。

---

## 8. ARTEX 对照与借鉴（2026-10 调研结论）

对标 `/home/kali/ARTEX`（Go 渗透智能体平台）后的角色映射与借鉴清单，作为上文 §3.1 / §4.1 的依据出处。

### 8.1 角色映射修正

| ARTEX | secpt 对应 | 修正说明 |
|---|---|---|
| MainAgent | **传令官职能 → 收编进网关（§3.1 策略编译闸）** | MainAgent **不是网关**：它是人机接口，把人类意图翻译成图写入。ARTEX 真正的网关是 intercept |
| intercept（PreToolUse 三值 allow/deny/ask + 审批页） | 网关 8 步门三态（§3） | 方向一致；ARTEX 的 ask 即我们的 escalate，其审批页对应我们的 escalation rail |
| planner | planner.orchestrate（规划—执行—重规划） | 同构 |
| worker 池 | 子智能体（带角色 prompt 的手，被动 ④⑤） | 同构 |
| 教练员 | — | secpt **独有**，保持不删 |
| 探索图黑板（fact / finding / goal / intent / asset 节点） | 账本投影出的图视图 | 见 §8.2 |
| ORIENTATION 触发（负反馈定向再探） | steer / blocked 重派 | 已有等价物 |

### 8.2 抄图的样子，不抄图的存储

ARTEX 图的价值在「一窗态势 + 溯源边（intent →yields→ finding）+ 只读继承」，但它是可变 DB 表存储（免不了 CAS 与重放水印）。secpt 的正确抄法：**从 append-only 账本投影出同款图视图**——图只是白板的另一种摆放，不是新的真相源；免 CAS（账本天然串行追加）、免水印重放（回放即重建）。

### 8.3 四条待收编项

| # | ARTEX 做法 | 落进 secpt |
|---|---|---|
| 1 | TriggerEvent debounce 合并 + 持久化水印 | 信号级唤醒（§4.1 v2）的实现样板：进入调度循环前先合流、带水位记账 —— **已实施**（`pa/runtime/wakeup.py`：窗口合流出 WakeUp + `wakeup.json` 水位，同轮合并/finding 置顶/兜底闹钟三纪律齐备） |
| 2 | 冷区 digest：图概览折叠老化内容 | E2 简报冷区压缩样板：L3 超长就 digest 摘要，账本原文永不丢 |
| 3 | 只读继承纪律：source_task_ids 只看直亲一层、带 inherited 标记、继承内容不可在别图落写 | L4 经验库（E3）边界规则：继承的经验条目打 `inherited` 标，只读不写，检索按任务类型过滤 |
| 4 | 溯源边 intent →yields→ finding | 事件 schema 演进：finding 事件带 origin（产出它的 directive/task），支撑 M3 出口审计与重复 finding 合并 |

---

## 9. sec-agent 对照与借鉴（2026-10 调研结论）

对标 `/home/kali/sec-agent`（Go + CloudWeGo Eino ADK 的会话级渗透智能体平台）后的适配判定。**总判：sec-agent 是会话级产品（一请求一轮、跑完即焚、无状态机无账本），secpt 是无人值守 mission 引擎——两者形态不同，它没有编排骨架可抄；能收编的清一色落在视图层与话术层，零个碰 runtime 状态机。**

### 9.1 画像要点

- 编排全部建立在 Eino ADK 预置编排上，三种模式：deep（task 委派子代理）、plan_execute（Planner→Executor→Replanner 固定循环）、supervisor（transfer→exit）；纪律靠 markdown 提示词 + 自研 middleware，无硬编码状态机。
- 作战室 = `project_facts` **可变 DB 存储**：同 key upsert 覆盖；索引（key+summary+溯源边+攻击路径）按 rune 预算注入 system prompt，body 落库须经 get_project_fact 显式取回；「边渗透边记录」靠 prompt 强制，无幂等键、无回放重建。
- HITL 三值门 reject 是**软工具结果**（模型自行调整后继续），无挂起审批、无 escalate——与我们「挂起审批」哲学相反。

### 9.2 值得收编项（过匹配关后修正版）

| # | sec-agent 做法 | secpt 现状 | 判定与落点 |
|---|---|---|---|
| 1 | 黑板索引/body 分层 + 预算 + 截断后检索引导 | `context._fit` 已有预算裁缝；L2/L3 视图已有 | 收编：缺的不是预算，是「索引当独立视图结构（key+summary+溯源边）」+「截断后提示去哪查」。落点 E1/E2，纯视图层。不引入 get_project_fact 式新工具（证据读取已统一走 store） |
| 2 | 子代理「缺目标回缺失清单、不得猜测」硬约束话术 | 机制已有（action.blocked 转机械门禁 + coach I2），但 worker.md 查无此话术 | 收编：worker.md 补一段硬约束话术，机制零改动 |
| 3 | 同 key upsert 合并重复发现 | upsert 存储撞 append-only 红线 | 换形态收编：合并语义下沉到 derive 投影——findings 台账投影时同 asset+fingerprint 取最新降杆合并（与质量项「重复 finding 合并」同工，加上 §8.3 的 origin 溯源边一起做） |
| 4 | reduction 大输出截断落盘证据化 | 形状已一致（工具出口→证据→store，子智能体不读 stdout） | 收编：只差一个超长输出截断阈值，小补在工具输出边界 |

### 9.3 不抄清单（形态或哲学冲突）

| # | sec-agent 做法 | 不抄理由 |
|---|---|---|
| 1 | deep / plan_execute / supervisor 三编排模式全 LLM 驱动 | secpt 编排靠机械骨架（planner.orchestrate + 门禁）；prompt 驱动编排无法通过 `_guard_*` 校验，重规划也无法保证串行幂等 |
| 2 | project_facts 可变 DB 存储（upsert 覆盖） | 撞 append-only 账本红线：可变真相源导致回放不可复现；正确形态是 §9.2-3 的投影合并 |
| 3 | HITL reject 即软工具结果 | 失去挂起裁决：reject 后模型自说自话继续跑，人不在环。secpt 的 escalate 挂起审批是强保证 |
| 4 | checkpoint / summarization / taskContextEnrich 运行时拦截 | 会话级产品每轮 context 即焚才有此需求；secpt 每轮视图从账本重建，E4 预算裁缝 + E2 简报是更优等价物；运行时拦截器与「一切经账本/视图」不合拍（其可收编语义已并入 §9.2-1 的派发卡 goal 指针） |
