# secpt · 项目架构

> 三份文档的分工：
> - **CONTROL_PLANE.md** —— 为什么这么设计（决策与理由）
> - **ARCHITECTURE.md**（本文）—— 是什么、怎么分层、模块边界、核心结构
> - **BUILD_SPEC.md** —— v0 怎么落地（文件级施工单 + 验收）
>
> 冲突时权威顺序：ARCHITECTURE > BUILD_SPEC > CONTROL_PLANE。
> 本文面向"由 AI Coding 按文档实现整个项目"，因此所有边界都写成**可机械检查**的形式。
> 配图：`views/page3_layers.svg`（§2/§3 分层与模块）、`views/page4_states.svg`（§6/§7.4 状态机与失败判定）。

---

## 目录

- [1. 系统全景](#1-系统全景)
- [2. 分层与依赖规则](#2-分层与依赖规则)
- [3. 模块树](#3-模块树)
- [4. 五个核心抽象](#4-五个核心抽象)
- [5. 核心数据结构](#5-核心数据结构)
- [6. 状态机](#6-状态机)
- [7. 关键时序](#7-关键时序)
- [8. 接口契约](#8-接口契约)
- [9. 扩展点](#9-扩展点)
- [10. 非功能要求](#10-非功能要求)
- [11. 风险与已知取舍](#11-风险与已知取舍)
- [12. 演进路线](#12-演进路线)
- [13. AI Coding 执行须知](#13-ai-coding-执行须知)

---

## 1. 系统全景

一句话：**一个可恢复的回合驱动器，驱动一个无状态决策大脑，把副作用全部收敛到一个网关，把结论收敛到一个裁决者。**

```
┌ L5 应用 ────────────────────────────────────────────────────────┐
│ CLI 入口 · Mission 立项 · 报告渲染 · 交付物打包                    │
└───────────────────────────┬─────────────────────────────────────┘
                            │ MissionSpec ↓        ↑ Deliverable
┌ L4 引擎 ──────────────────┴─────────────────────────────────────┐
│ 回合驱动（superstep）· 调度 · 预算 · 租约 · 检查点 · 恢复          │
│ 不思考：不拼 prompt、不调模型、不做业务判断                        │
└───────────────────────────┬─────────────────────────────────────┘
                            │ RunContext ↓         ↑ Decision（纯数据）
┌ L3 核心 ──────────────────┴─────────────────────────────────────┐
│ 上下文组装 · 决策 · 委派 · 有界 ReAct · 失败判定 · 证据校验        │
│ 无状态：state → decision，同一输入同一输出                        │
└──────┬───────────────────────────────────────────┬──────────────┘
       │ 轨迹/证据/指标                             │ 否决 / 裁决
┌──────▼──────────┐                       ┌─────────▼───────────┐
│ Mentor（教练）   │                       │ JEV（结果裁决）      │
│ 管过程、可否决    │                       │ 管结论、可要求补证    │
└─────────────────┘                       └─────────────────────┘
┌ L2 模型 ────────────────────────────────────────────────────────┐
│ 路由 · 重试降级 · 结构化输出 · 前缀缓存 · 成本归因                │
└───────────────────────────┬─────────────────────────────────────┘
┌ L1 基础设施 ──────────────┴─────────────────────────────────────┐
│ 事件账本 · 证据库 · 动作网关 · 工具执行 · 资产库 · 密钥           │
└─────────────────────────────────────────────────────────────────┘
```

三条贯穿全局的约束：

1. **副作用只有一个入口**（动作网关），**结论只有一个出口**（JEV + 报告）。
2. **状态只有一个写者**（引擎），其余全部是只读投影。
3. **判断可以交给模型，放行必须交给代码。**

---

## 2. 分层与依赖规则

### 2.1 五层职责

| 层 | 职责 | 不做什么 | 失败后果 |
|---|---|---|---|
| **L5 应用** | 立项、报告、交付 | 不做调度、不拼 prompt | 交付物结构漂移 |
| **L4 引擎** | 何时跑、跑谁、能否跑、存哪 | **不思考** | 状态不一致、重复执行 |
| **L3 核心** | 想什么、做什么、要不要拆 | **不持久化** | 上下文腐化、扇出失控 |
| **L2 模型** | 推理后端 | 不懂业务语义 | 成本失控、解析失败 |
| **L1 基础设施** | 副作用与持久化 | **不做判断** | 隔离失效、证据丢失 |

### 2.2 依赖规则（可机械检查）

```
规则 1  models / ports / errors 不依赖任何内部模块
规则 2  config 只依赖 models
规则 3  infra 只依赖 models / ports / errors
规则 4  tools 依赖 config / infra
规则 5  store / graph 依赖 models / config / infra        ← 见 2.3
规则 6  assets 依赖 tools（只读 manifest）
规则 7  gateway 依赖 store / infra / tools / config
规则 8  core 依赖 store(只读) / gateway / graph / assets / infra / ports
规则 9  runtime 依赖以上全部 + core
规则 10 mentor / jev / report 依赖 store(只读) / graph / models
规则 11 cli 依赖以上全部
```

**绝对禁止**：

| 禁止 | 检查方式 |
|---|---|
| engine/runtime 里出现 provider 名 | `grep -rE "openai\|anthropic\|httpx" pa/runtime/` |
| core 里出现持久化写操作 | core 只接收 `StoreView`，类型上就拿不到写方法 |
| gateway 之外的代码执行工具 | `grep -rn "REGISTRY\[" pa/ \| grep -v gateway` |
| 任何模块直接改 `state.tasks` 的状态字段 | 只有 `graph.transition()` 有写权限 |

### 2.3 关键调整：store 与 graph 下沉

**问题**：如果把事件账本放在"引擎"包里，核心层为了读状态就得 import 引擎，分层当场破掉。参考实现里就踩了这个坑（`core/worker.py` import `engine.gateway`）。

**修正**：把**存储机制**下沉到 L1/L2，引擎只保留**调度语义**。

```
L1  store/     事件账本、快照、投影        ← 机制
L2  graph/     任务图、状态机、ready 集合   ← 结构
L3  gateway/   动作网关                    ← 副作用收口
L4  runtime/   回合驱动、调度、预算、租约    ← 语义
```

同时把 store 拆成两个接口，用类型强制"单写者"：

```python
class StoreView(Protocol):        # 所有人可用（只读）
    def mission(self) -> MissionState: ...
    def events(self, from_seq: int = 1) -> Iterator[Event]: ...
    def evidence(self, eid: str) -> dict: ...

class StoreWriter(StoreView, Protocol):   # 只有 runtime 拿到
    def append(self, type: str, actor: str, payload: dict, idem_key: str) -> Event: ...
    def checkpoint(self, state: MissionState) -> None: ...
```

`core.decide(state, view, ...)` 的签名里根本没有 `append`——**分层不靠自觉，靠类型**。

### 2.4 层与包的映射

| 层 | 包 |
|---|---|
| L5 | `cli.py`、`report/` |
| L4 | `runtime/` |
| L3 | `core/`、`gateway/`、`policy/`（教练）、`adjudicate/`（JEV） |
| L2 | `infra/llm.py`、`assets/`、`graph/` |
| L1 | `store/`、`infra/`（evidence / executor）、`tools/` |

---

## 3. 模块树

```
secpt/
├── pyproject.toml
├── README.md
├── .env.example
├── pa/                        Python 包（短名）
│   ├── __init__.py
│   ├── errors.py              异常类型（禁止裸 Exception）
│   ├── models.py              ★ 全部数据契约（pydantic）
│   ├── ports.py               ★ 跨层协议（LLMClient / StoreView / Clock）
│   ├── config.py              Mission 加载、范围判定、时长解析
│   │
│   ├── store/                 L1 持久化机制
│   │   ├── event.py           Event 类型定义 + apply_event（纯函数）
│   │   ├── writer.py          StoreWriter 实现（引擎独占）
│   │   └── view.py            StoreView 实现（只读投影）
│   │
│   ├── infra/                 L1 副作用底座
│   │   ├── evidence.py        内容寻址证据库
│   │   ├── executor.py        子进程执行（文件重定向，不用管道）
│   │   ├── fp.py              动作指纹
│   │   └── llm.py             模型客户端（OpenAI 兼容 + Mock）
│   │
│   ├── tools/                 L1 可执行能力
│   │   ├── __init__.py        ToolSpec + REGISTRY + manifest
│   │   ├── shell.py           受限 shell（命令白名单）
│   │   ├── http.py            单次 HTTP，不跟随重定向
│   │   ├── fs.py              workspace 内文件读写
│   │   └── probe.py           TCP 探活（确定性线索来源）
│   │
│   ├── graph/                 L2 任务图
│   │   ├── task.py            create_task
│   │   ├── state.py           ALLOWED 转移表 + transition
│   │   └── ready.py           preconditions / deps / ready 集合
│   │
│   ├── assets/                L2 资产
│   │   ├── skills.py          技能加载（markdown + front matter）
│   │   ├── snapshot.py        资产快照（mission 级冻结）
│   │   └── tools.py           工具 manifest → prompt 片段
│   │
│   ├── gateway/               L3 动作网关（PEP）
│   │   ├── scope.py           范围守卫
│   │   ├── policy.py          风险档 / 策略加载（PDP）
│   │   └── execute.py         执行 + 去重 + 证据落盘 + 审计
│   │
│   ├── policy/                L3 教练（过程）
│   │   ├── review.py          计划结构检查（纯代码）
│   │   ├── signals.py         漂移指标（纯计算）
│   │   └── intervene.py       I1~I5 梯度与升级规则
│   │
│   ├── core/                  L3 决策大脑
│   │   ├── context.py         ★ 上下文组装（四段 + 窗口裁剪）
│   │   ├── planner.py         首个计划 / 重规划
│   │   ├── decide.py          core_decide
│   │   ├── delegate.py        简报编译
│   │   ├── worker.py          ★ 有界 ReAct
│   │   ├── reflect.py         判据 / 线索抽取 / 失败判定 / 证据校验
│   │   └── prompts/           core.md worker.md planner.md mentor.md jev.md
│   │
│   ├── adjudicate/            L3 结果裁决（JEV）
│   │   ├── rules.py           规则集（可扩展）
│   │   └── adjudicator.py     裁决主流程
│   │
│   ├── runtime/               L4 回合驱动
│   │   ├── loop.py            ★ superstep
│   │   ├── scheduler.py       派发 / 归并 / 判定落地
│   │   ├── budget.py          分层预算
│   │   ├── lease.py           租约
│   │   └── recovery.py        快照恢复 + 租约重算 + 孤儿进程
│   │
│   ├── report/                L5 交付
│   │   ├── render.py          从状态渲染 markdown（幂等）
│   │   └── bundle.py          证据包打包
│   │
│   └── cli.py                 L5 入口
│
├── skills/                    技能库（markdown）
├── missions/                  运行时数据（gitignore）
└── tests/
```

**★ = 契约关键文件，改它们等于改架构。**

---

## 4. 五个核心抽象

| 抽象 | 本质 | 写者 | 读者 | 生命周期 | 不变量 |
|---|---|---|---|---|---|
| **Mission** | 一次交战 | CLI | 全部 | 小时~天 | 唯一 id + 冻结的资产快照 + 固化验收项 |
| **Task** | 调度单元 | 引擎 | core / worker | 秒~小时 | 状态只能由 `graph.transition()` 改 |
| **Decision** | 一次决策 | core / worker | 引擎 | 瞬时 | 纯数据，可序列化，可回放 |
| **Evidence** | 原始产物 | 网关 | 全部 | 永久 | 内容寻址、不可变、可校验 |
| **Finding** | 结论 | worker / core | JEV / 报告 | 战役内 | 必须挂 evidence_ref，且可溯源 |

四者关系：

```
Mission 1 ── N Task
Task    1 ── N Evidence（每次动作一条）
Task    1 ── N Finding（一个任务可产出多条结论）
Finding N ── M Evidence（多对多，至少 1 条）
Finding 1 ── 1 Adjudication
```

---

## 5. 核心数据结构

### 5.1 Task

```python
Task = {
  id, mission_id, type, goal,
  parent, deps[], preconditions[],          # 依赖
  success_criteria[], abort_criteria[],     # 判据（指向证据类型）
  scope_slice{}, allowed_tools[],           # 作用域与能力子集
  budget{steps, seconds, tokens, risk},     # 硬顶
  priority, state, lease, attempt,
  tried[],                                  # 已试指纹（跨尝试继承）
  lineage{supersedes, superseded_by[]}      # 重启血缘
}
```

**三条不变量**：
1. `success_criteria` 必须指向证据类型，不能是形容词——否则无法判定完成。
2. `tried` 跨尝试继承——否则重启会重走老路。
3. `lineage` 只增不删——攻击路径追溯依赖它。

### 5.2 Decision（判别联合）

```python
Decision = CallTool | Spawn | Replan | Wait | Finish
```

| 类型 | 语义 | 必须字段 |
|---|---|---|
| `CallTool` | 调一个工具 | `tool, args, expect` |
| `Spawn` | 派生子任务 | `tasks[]` |
| `Replan` | 改计划 | `ops[]` |
| `Wait` | 等后台任务 | `job_id` |
| `Finish` | 收口 | `summary, satisfied[]` |

**`expect` 是反思的锚点**：没有预期就没有"预期 vs 实际"的差异，也就无法判断该不该换战术。

### 5.3 Event（唯一真源）

```python
Event = { seq, mission_id, ts, type, actor, payload, idem_key }
```

事件类型分四组：

| 组 | 事件 |
|---|---|
| 生命周期 | `mission.created` `mission.finished` `plan.created` `plan.revised` |
| 任务 | `task.created` `task.state_changed` |
| 动作 | `action.started` `action.finished` `action.blocked` `evidence.stored` |
| 认知 | `decision.made` `clue.added` `finding.proposed` `finding.adjudicated` |
| 治理 | `mentor.intervened` `restart.decided` `budget.charged` `human.directive` |

**投影规则**：只有 6 类事件参与状态重建，其余只落账本（审计用）。

| 参与投影 | 不参与 |
|---|---|
| task.created / state_changed / clue.added / finding.proposed / finding.adjudicated / mission.finished | decision.made / action.* / mentor.* / budget.* |

### 5.4 WorkerResult

```python
WorkerResult = {
  task_id, status,                 # done|partial|failed|blocked|needs_help
  clues[], findings[], evidence_refs[],
  tried[], confidence, cost{},
  proposals[],                     # 只能"提议"新任务，不能写 DAG
  failure{cls, detail, evidence_ref}
}
```

### 5.5 Adjudication

```python
Adjudication = {
  finding_id, verdict,             # confirmed|suspected|false_positive
  verdict_by, reason,
  counter_evidence[], unlock
}
```

---

## 6. 状态机

### 6.1 任务状态机（唯一的转移表）

```
pending ──► ready ──► running ──┬──► done
   │           │                ├──► failed ──► ready（重启）
   │           │                ├──► blocked
   │           │                └──► ready（租约过期）
   └───────────┴────────────────────► superseded
```

```python
ALLOWED = {
  "pending":    {"ready", "superseded"},
  "ready":      {"running", "superseded"},
  "running":    {"done", "failed", "blocked", "ready", "superseded"},
  "failed":     {"ready", "superseded"},
  "done":       {"superseded"},
  "blocked":    {"superseded"},
  "superseded": set(),
}
```

**守卫**：每次转移写一条 `task.state_changed`，非法转移抛 `IllegalTransition` 而不是静默修正。

### 6.2 战役状态机

```
created ─► running ─┬─► completed          验收项全部有证据
                    ├─► budget_exhausted   token/时间/步数触顶
                    ├─► plateau            连续 8 超级步无新事实
                    ├─► halted             熔断 / 人工叫停
                    └─► failed             不可恢复错误
```

### 6.3 Finding 生命周期

```
proposed ──► adjudicated(confirmed | suspected | false_positive)
                    │
                    └─► reopened（补证后重新裁决，最多 2 次）
```

### 6.4 Evidence

只增不改。删除只能是"标记删除"，物理文件保留。

---

## 7. 关键时序

### 7.1 冷启动

```
CLI ──► 读 targets/acceptance ──► 生成 MissionSpec ──► 写 mission.json
    ──► 冻结资产快照 ──► 写 mission.created
    ──► planner: objective → PlanDraft（1~8 个任务）
    ──► 教练结构检查（I4 则打回重规划，最多 3 轮）
    ──► 建 Task（pending）──► 进入超级步
```

### 7.2 超级步

```
┌─ loop ────────────────────────────────────────────────────────┐
│ 1. 终止判定（验收达成 / 预算 / 平台期 / 熔断 / 人工）           │
│ 2. 租约过期回收                                                │
│ 3. 教练 tick：算漂移 → I1 静默 / I3 提醒 / I4 否决              │
│ 4. 主控决策：decide(RunContext) → Decision                     │
│ 5. 执行决策：call_tool 走网关 | spawn 建任务 | replan 改图      │
│ 6. 派发 ready 任务（≤并发上限），同步跑 worker                 │
│ 7. 归并结果 → 失败判定 → 状态落定 → JEV 扫描                   │
│ 8. 结算成本 → 检查点 → step++                                  │
└────────────────────────────────────────────────────────────────┘
```

**批内不相见**：同一超级步内的任务看不到彼此产出。这是 ready 集合（前置已满足）来保证的，也是并行正确性的前提。

### 7.3 委派

```
core ──► Spawn(tasks)
     ──► 教练校验独立性（共享会话/凭证 → I4 拒绝）
     ──► 建任务 → 派发
     ──► compile_briefing(task)  ← 只给结论与指纹，不给推理过程
     ──► worker 有界 ReAct（工具子集 + 预算硬顶）
     ──► WorkerResult ──► 归并
```

**父不等子**：派发后父继续走主线，子完成时发事件唤醒。

### 7.4 失败判定与重启

```
worker 返回
   │
   ├─ 自报 done + 判据满足 ──► success ──► done
   ├─ 自报 done + 判据不满足 ► virtual_success ──► failed（不重试，计入质量账本）
   ├─ 自报 failed + 不满足 ──► failure ──► 归因
   ├─ 自报 failed + 满足 ────► false_failure ──► done（标记 worker 不可靠）
   ├─ blocked ──────────────► blocked（不重试）
   └─ partial ──────────────► failed（保留已得）
                │
                ▼
        教练判"是不是任务失败"
                │
                ▼
        父决定"怎么重新开始"（六选一）
         修订判据 / 拆分 / 插前置 / 扩权 / 弃分支 / 退避重排
                │
                ▼
        重启：重置执行状态，保留线索·证据·已试指纹·血缘
```

### 7.5 裁决

```
finding.proposed
   │
   ├─ 无证据 / 证据不可校验 ──► false_positive
   ├─ 缺 falsify_if ──────────► suspected（unlock: 补 falsify_if）
   ├─ 单条证据 ──────────────► suspected（unlock: 补独立证据）
   ├─ 无原始输出 ────────────► suspected（unlock: 补原始输出）
   └─ 多证据 + 含原始输出 ───► confirmed
```

### 7.6 崩溃恢复

```
启动
 ├─ 有快照 → 加载快照（seq=N）
 ├─ 否则   → 事件全量重放
 ├─ 重放 N+1.. 的事件（apply_event 纯函数）
 ├─ 租约过期 → running 改回 ready
 ├─ 孤儿进程 → 重连或终止（job registry）
 └─ 继续超级步
```

**幂等保证**：动作指纹在 `action.started` 时登记；重放时同指纹不再执行。

---

## 8. 接口契约

按层给签名。**AI Coding 应按这些签名建模块，签名不稳不要改。**

### L1 存储

```python
# store/writer.py
class StoreWriter:
    def __init__(self, root: Path) -> None: ...
    def append(self, type: str, actor: str, payload: dict, idem_key: str) -> Event: ...
    def checkpoint(self, state: MissionState) -> None: ...
    def snapshot_now(self, state: MissionState) -> None: ...
    def seen_action(self, fp: str) -> bool: ...
    def action_evidence(self, fp: str) -> str | None: ...

# store/view.py
class StoreView:
    def restore(self) -> MissionState: ...
    def events(self, from_seq: int = 1) -> Iterator[Event]: ...
    def exists(self) -> bool: ...

# store/event.py
def apply_event(state: MissionState, ev: Event) -> None: ...   # 纯函数
```

### L1 基础设施

```python
# infra/evidence.py
def put(root: Path, content: dict, meta: dict | None = None) -> str: ...
def get(root: Path, eid: str) -> dict: ...
def verify(root: Path, eid: str) -> bool: ...

# infra/executor.py
def run_command(cmd: str, timeout: int = 120, cwd: Path | None = None) -> ProcResult: ...

# infra/fp.py
def fingerprint(tool: str, args: dict) -> str: ...

# infra/llm.py
def parse_json(raw: str, schema: type | None = None) -> Any: ...
def load_client(mock_script: str | None = None) -> LLMClient: ...
```

### L1 工具

```python
# tools/__init__.py
@dataclass
class ToolSpec:
    name: str
    risk_tier: str          # low|med|high|critical
    side_effect: str        # none|read|write|destructive
    requires: list[str]     # 前置线索，喂"补前置"
    schema: dict
    produces_evidence: bool
    fn: Callable[[dict, ToolContext], ProcResult]

REGISTRY: dict[str, ToolSpec]
def manifest() -> list[dict]: ...
```

### L2 任务图

```python
# graph/task.py
def create_task(state, writer, draft: TaskDraft) -> Task: ...

# graph/state.py
def transition(task, to: str, writer, *, lease=None, attempt=None) -> None: ...

# graph/ready.py
def ready_tasks(state) -> list[Task]: ...
def promote_pending(state, writer) -> int: ...
```

### L3 网关

```python
# gateway/execute.py
@dataclass
class Outcome:
    ok: bool
    blocked_reason: str | None
    evidence_id: str | None
    stdout: str
    stderr: str
    exit_code: int
    cached: bool
    risk: str

def execute(state, writer, *, tool_name, args, task_id,
            allowed_tools: list[str] | None = None) -> Outcome: ...
```

**顺序固定**：存在性 → 工具子集 → 范围 → 风险档 → 去重 → 执行 → 证据落盘 → 审计。

### L3 核心

```python
# core/context.py
def assemble(phase: str, state, payload: dict,
             assets: AssetSnapshot) -> tuple[str, str]: ...   # (system, user)

# core/decide.py
def core_decide(state, view, llm, assets) -> Decision: ...

# core/planner.py
def bootstrap_plan(state, writer, llm, assets) -> PlanDraft: ...

# core/worker.py
def run_worker(task: Task, state, writer, llm,
               assets) -> WorkerResult: ...

# core/reflect.py
def criteria_met(clues: list[Clue], criteria: list[Criterion]) -> bool: ...
def judge(task: Task, result: WorkerResult, state) -> str: ...
def validate_evidence(root: Path, refs: list[str], task_id: str | None = None) -> list[str]: ...
```

### L3 教练 / 裁决

```python
# policy/review.py
def review_plan(state, tasks: list[TaskDraft]) -> list[Issue]: ...

# policy/signals.py
def drift(state) -> dict[str, float]: ...
def tick(state, writer) -> str: ...       # 返回介入级别 I1~I5

# adjudicate/adjudicator.py
def adjudicate(state, writer, root: Path, finding: FindingProposal) -> Adjudication: ...
def sweep(state, writer, root: Path) -> int: ...
```

### L4 引擎

```python
# runtime/loop.py
def run(mission_dir: Path, llm: LLMClient,
        assets: AssetSnapshot | None = None) -> MissionState: ...

# runtime/scheduler.py
def dispatch_ready(state, writer, llm, assets) -> int: ...
def merge_result(state, writer, task, result) -> int: ...

# runtime/budget.py
def mission_exhausted(state) -> str | None: ...
def charge(state, writer, tokens: int, seconds: int = 0) -> None: ...

# runtime/lease.py
def expire(state, writer) -> int: ...
```

---

## 9. 扩展点

| 想加什么 | 改哪里 | 不要做什么 |
|---|---|---|
| **新工具** | 在 `tools/` 加一个模块，声明 `ToolSpec`，在 `__init__` 注册 | 不要在 gateway 里加 if-else |
| **新角色（侦察/Web/AD）** | 只改 `allowed_tools` + 注入的技能 + prompt 片段 | 不要写新的 agent 类；角色是配置不是代码 |
| **新技能** | `skills/` 加一个 md，写 `trigger` | 不要让技能绕过门控直接写回 |
| **新裁决规则** | `adjudicate/rules.py` 加一个规则函数 | 不要改 `adjudicator` 主流程 |
| **新存储后端** | 实现 `StoreView` / `StoreWriter` | 不要改 `apply_event` 的语义 |
| **新场景（CTF / BAS）** | 改 `MissionSpec` 字段与 preset | 不要引入 profile 继承体系 |
| **新模型供应商** | 实现 `LLMClient.complete` | 不要让 runtime 感知供应商 |

**扩展点的共同原则**：新增能力走"注册"，不走"分支"。任何 `if tool == "xxx"` 都是在把扩展点焊死。

---

## 10. 非功能要求

### 10.1 上下文预算

| 项 | 上限 | 理由 |
|---|---|---|
| system 前缀 | 逐字节稳定 | prefix cache 命中率决定成本 |
| 单次 user | 120k 字符（≈30k token） | 留足推理空间 |
| 线索窗口 | 最近 60 条 + 20k 字符 | 工作记忆是**窗口**，不是全量 |
| 工具清单 | ≤20 个 | 超过会让模型乱选 |

### 10.2 规模与性能

| 项 | 目标 |
|---|---|
| 单 mission 超级步 | ≤ 200 |
| 单任务步数 | ≤ 12（默认），≤ 50 上限 |
| 并发 worker | ≤ 8，同主机写操作串行 |
| 事件重放 | ≤ 1 万条 < 5 秒 |
| 单条事件 | ≤ 4KB（大内容进证据库） |

### 10.3 成本结构

| 角色 | 占比目标 | 说明 |
|---|---|---|
| 主控 | 主体 | 每超级步 1 次 |
| worker | 视任务数 | 受限步数 |
| mentor | ≤ 3% | 关口制，O(阶段) 不是 O(步) |
| JEV | ≤ 5% | 每 finding 1 次（v0 纯规则，≈0） |
| 收口预留 | ≥ 5% | 报告与复现必须有钱做 |

### 10.4 可靠性

- 每条事件 `fsync`；每 200 条一份快照。
- 动作幂等：同指纹不重复执行。
- 恢复后 `running` 任务按租约重算，不假设"还在跑"。
- 模型调用失败最多重试 1 次，再失败切 fallback，切换是显式事件。

### 10.5 安全

| 要求 | 落点 |
|---|---|
| 范围守卫 | 网关，逐跳复验；空范围一律拒绝 |
| 不可逆动作 | production 下默认挂起 |
| 能力子集 | 任务声明的工具集是上限，运行时只能减 |
| 证据完整性 | 内容寻址 + 哈希校验 |
| 跨客户隔离 | mission 目录物理隔离 + 检索强制带 mission 过滤（代码层） |
| 注入防护 | 目标回显进 `<untrusted>` 区块，与指令区物理分离 |

### 10.6 可观测性

每个超级步一条汇总事件，包含：step、tokens、任务状态分布、线索增量、介入级别。有了它，"为什么这一步走了这么久"才有答案。

---

## 11. 风险与已知取舍

| # | 风险 | 现实影响 | v0 取舍 |
|---|---|---|---|
| 1 | 同步执行导致墙钟被最长任务拖住 | 并发收益无法兑现 | 先正确后快，M3 换进程池 |
| 2 | 模型输出不稳定，解析失败 | 单步失败，不致命 | 重试 1 次 + fallback + 严格 schema |
| 3 | 证据体积膨胀（原始终端输出） | 几天任务可达 GB | 内容寻址去重 + 单条上限 + 冷归档 |
| 4 | 误报责任在签发人 | 一次误报毁掉信任 | JEV 规则化 + 反证字段 + 人审接口 |
| 5 | 长任务漂移 | 方向每天变一次 | `current_strategy` 显式化 |
| 6 | 供应商中断 | 几天任务必挂 | 异厂商 fallback + 显式中断事件 |
| 7 | 跨客户污染 | 合规事故 | 物理隔离，不靠提示词 |
| 8 | 目标反向攻击（AI 红队场景） | 提示注入改行为 | untrusted 区块 + 凭据不入 prompt |
| 9 | 教练过度否决 | 任务停滞 | 否决必须带替代，否则降级为异议 |
| 10 | 任务图膨胀 | 死节点污染调度 | superseded + 同源链路压缩 |

**一条元风险**：机制数量已经超过 20 个，而每个机制的收益都没被独立验证。v0 的价值是**尽早让现实打一次**，而不是把机制做全。

---

## 12. 演进路线

| 里程碑 | 目标 | 验收 |
|---|---|---|
| **M0 牙签** | 1 靶 → 1 链路 → 1 条带证据的 finding + 报告 | 端到端跑通；`kill -9` 后可续；第 200 步 prompt 与第 20 步同量级 |
| **M1 裁决** | JEV 规则 + 全量证据 + 溯源校验 | 误报率下降；编造证据被拦 |
| **M2 评测** | 靶集 + baseline + 回归 | 指标优于 plain agent 基线 |
| **M3 并行** | 扇出 + 租约 + 干扰粗规则 | 8 并发无重复动作、无越界 |
| **M4 持久** | 完整事件溯源 + 快照 + 孤儿清理 | 跨天任务可续 |
| **M5 教练** | I1~I5 梯度 + 漂移检测 + 计划评审 | 漂移能在失败前被拦住 |
| **M6 自演进** | 经验 → 技能（门控） | 同靶二刷指标提升 |

**顺序的理由**：先证明"结论对不对"，再证明"跑得快不快"，最后证明"跑得久不久"。反过来做，会在一个不可信的系统上堆性能。

---

## 13. AI Coding 执行须知

### 13.1 实现顺序

```
S1  models + ports + errors           → 能 import
S2  store（event/writer/view）        → 事件幂等 + 恢复通过
S3  graph（state/ready/task）         → 非法转移抛错
S4  infra（evidence/executor/fp）     → 哈希稳定、超时可杀
S5  tools（probe/http/fs/shell）      → 各自可单独调用
S6  gateway                           → 越界拦截
S7  infra/llm（含 Mock）              → 无网络可跑
S8  core/context                      → prompt 尺寸稳定
S9  core/reflect + worker             → 单任务能判成功/失败
S10 runtime（loop/scheduler/budget/lease）
S11 adjudicate + report + cli         → 端到端
S12 policy（教练）                     → 越界计划被拦
```

**S1–S7 期间不要写 `runtime/loop.py`。** 先把零件测通再装配，否则出问题无法定位是零件错还是装配错。

### 13.2 六个必踩的坑（已由参考实现验证）

| 坑 | 现象 | 正确做法 |
|---|---|---|
| 判别联合当模型用 | `Decision.model_validate` 报 `AttributeError` | 用 `TypeAdapter(Decision).validate_python()` |
| 引擎引模型客户端 | 分层检查失败 | 协议放 `ports.py`，实现放 `infra/llm.py` |
| 工作记忆做成全量 | 第 100 步 prompt 涨 3.7 倍 | 线索按**条数窗口**（60）+ 字符预算双限 |
| 子进程用管道 | Windows 沙箱下 spawn 被拒 | 用文件重定向，不用 `capture_output` |
| 系统临时目录 | 沙箱重定向后不可写 | 运行数据统一放工作区内 |
| 中文引号写进字符串 | 语法错误 | 文案里不要用 ASCII 双引号嵌套 |

### 13.3 禁止事项

1. 不要引入 LangChain / LlamaIndex / AutoGen 等框架。
2. 不要在 `runtime/` 里拼 prompt 或 import provider。
3. 不要在 `core/` 里调用 `writer.append()`（类型上就拿不到）。
4. 不要用 `if tool == "xxx"` 扩展能力。
5. 不要为"以后可能有用"加参数。
6. 不要在没有验收测试前加第二个存储后端。
7. 不要在 v0 做并发、Web UI、多租户、图数据库。

### 13.4 每个模块的完成定义

一个模块算完成，必须同时满足：

- [ ] 签名与 §8 一致；
- [ ] 有对应的验收测试（§15 of BUILD_SPEC）；
- [ ] 不违反 §2.2 依赖规则；
- [ ] 异常走 `errors.py`，不抛裸 `Exception`；
- [ ] 写磁盘的地方做了 `fsync`。

---

## 附录：与参考实现的对照

仓库内 `pa/` 下有一份 v0 参考实现（尚未按本文 §2.3 下沉 store/graph），差异只在包名：

| 本文 | 参考实现 |
|---|---|
| `pa/store/` `pa/graph/` | `pa/engine/store.py` `pa/engine/taskgraph.py` |
| `pa/runtime/` | `pa/engine/loop.py` `scheduler.py` `budget.py` `lease.py` |
| `pa/gateway/` | `pa/engine/gateway.py` |
| `pa/policy/` | `pa/mentor/` |
| `pa/adjudicate/` | `pa/jev/` |

**以本文为准。** 参考实现的逻辑可直接搬，改 import 路径即可；但 `StoreWriter/StoreView` 的拆分要新写（参考实现是单一 `Store`）。

### 附录 A：命名约定

| 项 | 值 |
|---|---|
| 项目名 / 产品名 | `secpt` |
| CLI 命令 | `secpt` |
| Python 包（导入名） | `pa`（短名） |
| 环境变量前缀 | `SECPT_` |
| 异常基类 | `SecptError` |
| 默认运行目录 | `missions/` |

包目录名 `pa` 与项目名 `secpt` 不一致是有意保留的短导入名。若要统一，执行两步（**当前环境禁止重命名目录，需在你本机执行**）：

```powershell
# 1) 重命名包目录
Rename-Item -LiteralPath '<项目根>\pa' -NewName 'secpt'

# 2) 全局替换导入路径与入口点
#    from pa.  -> from secpt.
#    import pa -> import secpt
#    pa.cli:main -> secpt.cli:main
```

重命名后，`pyproject.toml` 的 `[project.scripts]` 与 `python -m` 调用需同步更新；文档中出现的 `pa/` 一律读作 `secpt/`。
