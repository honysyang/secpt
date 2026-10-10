# secpt 渗透测试智能体 · 开发文档

> 工程代号：`secpt`　|　工作目录：`/home/kali/secpt`　|　文档版本：**v0.3**　|　更新日期：2026-10-05
>
> 建议产品名「章鱼 Octopus」，与公司既有产品线（水獭 / 湖鲟 / 河豚）同属水生家谱；工程代号 `secpt` 保留为仓库名与 CLI 名。

### 版本记录

| 版本 | 变更 |
|---|---|
| v0.1 | 初版：技术选型、模块契约、硬约束 |
| v0.2 | 补 Phase 1 实现状态、真实 DeepSeek trajectory、测试基线 |
| **v0.3** | 确立**「千里马原则」（能力优先、治理后置）**；新增第 2 章设计原则；新增**第 6 章能力主线**（Node 树 / 波前 / Evaluator / 面收缩落地 / 工具矩阵 / 端到端推演）；新增第 8 章**治理债台账**；路线图重排为能力主线优先 |

---

## 0. 文档定位

| 文档 | 作用 |
|---|---|
| 《渗透智能体开发文档》（桌面设计蓝图） | 定义**目标**：做成什么样（分形树、波前、死亡证明、面收缩） |
| **本文档** | 定义**落地**：怎么一步步做出来（技术选型、模块契约、能力主线设计、硬约束、路线图） |

> **实现状态标记**：✅ 已实现并测试通过　🚧 方案已定、实现中　⬜ 待实现

---

## 1. 愿景与产品定位

### 1.1 设计原点：从「线」到「面」

传统渗透专家团队做安全 assessment，是**一条线一条线地验证、收集、分析，逐步缩小范围**往下走。AI 的本质优势不在于把这条线走得更快，而在于——

> **同时从多条战线推进，形成一个「面」，再逐步收缩这个面。**

### 1.2 面收缩三机制

| 机制 | 触发 | 行为 | 确定性 |
|---|---|---|---|
| ① 死路排除 | 某节点撞死路 | 发**死亡证明** + 把「此路已排除」写进**全局排除清单** → 兄弟及后续节点读图时自动绕开 | 确定性杀死 |
| ② 重排预算 | 某节点拿到关键证据（如凭证） | **唤醒父节点** → 父节点重排，把预算从「还在瞎逛的兄弟」抽给「能顺着凭证往下打的活路」 | 确定性 |
| ③ 经验收敛 | AI 判断当前漏洞/特征高度匹配某种渗透经验 | 评估并**抽走**其他渗透尝试的预算 | 概率性 |

**护栏约定**：机制 ③ 采用 **只降级不杀、可复活** 策略——被降级节点预算归零但保留在图中，与 `Hypothesis` 生命周期绑定，上游出现新证据即可复活。与机制 ①② 的不可逆处理严格区分。

### 1.3 「千里马原则」：先能力、后治理 ★ 本次确立的产品决策

> **先把这匹千里马养出来——让它在真实目标上真的能打、能收敛、能出结果。治理护栏后置，等能力与 stability 验证通过后再补。**

这条原则直接决定了排期与取舍：

| 类别 | 内容 | 优先级 |
|---|---|---|
| **能力主线** | 多线并行（Node 树 + 波前）、证据驱动的 WorldModel、Evaluator 预算分配、面收缩三机制、真实工具矩阵、症状级别的抗幻觉 | **最高，现在就做** |
| **治理护栏** | 目标授权范围（Scope）、速率限制、高危动作人工确认、破坏性动作分级、审计持久化 | **有意后置**，见 §8 治理债台账 |

**为什么可以这样排**：这些治理项里的绝大多数**不是架构性约束**——Scope 是在执行边界外加一层校验，限速是改并发令牌，高危确认是在 Tool 调用前插一道闸门，都不会重构已经写好的 Node 树或调度器。所以后置成本可控。

**但必须留清边界**（这些是**现在就要守住、不属于可后置治理**的安全底线，写进 §7 红线）：密钥不落盘、禁 `shell=True`、白名单执行、命令超时、structlog 审计。

> ⚠️ **明确记录**：v0.3 已把「目标授权范围 Scope 缺失」识别为红线级风险并主动降级为治理债。这是**产品决策**，不是缺陷被遗忘——触发条件见 §8。

---

## 2. 设计原则

这六条是所有后续设计的判断依据，任何新增模块都必须满足：

1. **证据纪律（Evidence Discipline）**——图上任何结论必须挂载 `evidence_id`。**没有证据，不得写入实体/边/结论。** LLM 的推理只能生成 `Hypothesis`（待验证假设），不能直接降级成事实。这是整个系统抗幻觉的根。
2. **六拍循环**——每个 Node 的行为被收敛为固定六拍：感知（读图）→ 假设 → 行动 → 观察 → 评估 → 记账（写图）。强制同构，任意 Node 可递归展开成子 Node（分形）。
3. **信任传播**——信任从源头证据的 `trust_tier` 取，沿边传播取最小值；含推理成分的边必须封顶并标 `unverified`。详见 §7.4。
4. **推测执行 + Checkpoint**——允许基于假设提前往下探（抢占机会窗口），但必须打 checkpoint；假设被证伪时**整体回滚**该分支产生的图状态变更。
5. **可观测先于优化**——「某方向已枯竭」这类东西无法直接测量，必须设计**代理指标**（见 §6.9）。不测量就谈优化等于猜。
6. **一切可控可审计**——停止条件、预算分配、死亡判定必须是显式代码逻辑，**不能寄托于 LLM 自觉**。LLM 负责产生候选，系统负责裁决。

---

## 3. 技术选型（Phase 0）

| 维度 | 选择 | 理由与备注 |
|---|---|---|
| 语言 | **Python 3.12** | 生态、asyncio 成熟度；type hints 全程使用 |
| Agent 框架 | **自研 ReAct**（不引 LangGraph） | 需要**完全可控的停止条件 / 证据门控 / 审计留痕**；LangGraph 抽象过重、内部状态难以审计，与原则 6 冲突 |
| LLM 网关 | **litellm → DeepSeek** | 屏蔽 provider 差异，后续换模型改动最小 |
| 模型分工 | V3 `deepseek-chat` 规划 / R1 `deepseek-reasoner` 评估 | 成本与推理深度分离；未订阅 R1 时 `eval_model` 可与 planning 同值 |
| 并发 | **asyncio** | 波前并行的基础设施；工具执行全部 coroutine 化 |
| 存储 | **GraphStore 三档缝**：内存 → SQLite → Postgres | 先跑起来不依赖外部服务，成型后平滑升级；`GraphConfig.backend` 切换 |
| 配置 | **pydantic-settings + YAML** | 类型安全 + 分层覆盖 |
| 日志 | **structlog** | 结构化事件，便于 trajectory 回放与分析 |
| CLI | **typer** | 轻量、类型推导命令行 |
| 测试 | **pytest + pytest-asyncio（`asyncio_mode=auto`）** | 异步用例免装饰器 |
| 工具执行 | **白名单 subprocess** | 安全底线：只允许白名单命令，禁止 shell 注入 |
| ORM（4b 起） | **SQLAlchemy 2.x async** + aiosqlite / asyncpg | 三档后端演进路径 |

**三个已确认取舍**：① 自研、不引 LangGraph；② 主库演进**优先 Postgres**，不直接上 Neo4j（邻接表/CTE 足以表达，复杂度可控）；③ 走 litellm，不用各厂商原生 SDK。

> ⚠️ 与另一并行项目（离线 CPU 大模型部署，Qwen3-30B-A3B）**完全独立**，模型与技术路线不串味、不复用。

---

## 4. 实现状态总览

| 阶段 | 内容 | 状态 | 测试基线 |
|---|---|---|---|
| **Phase 0** | 脚手架 + 技术选型 + 配置/日志/CLI 骨架 | ✅ | — |
| **Phase 1 · A** | `core/llm` 适配层（litellm + FakeLLM + 工厂） | ✅ | `test_llm.py` 6 passed + 1 skipped |
| **Phase 1 · B** | `runtime/subprocess.py` 白名单执行 | ✅ | `test_subprocess.py` 8 passed |
| **Phase 1 · C** | `core/tools` 抽象 + `core/react` 最小 ReAct 循环 | ✅ | `test_react` + `test_tools` 11 passed |
| **Phase 1 · 3b** | 真实 DeepSeek 打通（few-shot + 真模型冒烟） | ✅ | 有 key 冒烟 2 passed；无 key 全量 26 passed / 3 skipped |
| **Phase 1 · 4a** | `GraphStore` 抽象 + `MemoryGraphStore` + 接进 runner | 🚧 | 方案已定，待回报 |
| **Phase 1 · 4b/4c** | SQLite 后端 / Postgres 后端 | ⬜ | — |
| **Phase 2 · 能力主线** | Node 树 / Genome / Scheduler / Wavefront / Evaluator / 面收缩 | ⬜ | 见 §6 |
| **Phase 3** | 真实工具矩阵、策展知识库、报告 | ⬜ | — |
| **Phase 4 · 治理** | Scope 授权、限速、高危确认、破坏性分级、审计落地 | ⬜ | 见 §8 |

---

## 5. 已实现模块详细设计

### 5.1 配置层 `src/secpt/config`

**优先级链**：环境变量（`SECPT_` 前缀，嵌套 `__`） > `config.yaml` > 代码默认值。

```python
# models.py
class LLMConfig(BaseModel):
    provider: str = "deepseek"
    planning_model: str = "deepseek-chat"     # V3，规划
    eval_model: str = "deepseek-reasoner"      # R1，评估
    api_key_env: str = "DEEPSEEK_API_KEY"      # ← 环境变量「名」，不是 key！
    temperature: float = 0.2
    max_tokens: int = 2048
    timeout: float = 60.0
    fake: bool = False                          # True ⇒ 工厂返回 FakeLLM

class ToolsConfig(BaseModel):
    whitelist: list[str] = []                   # 命令基名列表，如 ["echo","nmap","dig"]
    default_timeout: float = 30.0

class GraphConfig(BaseModel):
    backend: str = "memory"                     # memory / sqlite / postgres
    sqlite_path: str = "secpt.db"
    dsn: str = ""
```

> ⚠️ **`settings.py` 陷阱**：`YamlConfigSource` 是**自定义** `PydanticBaseSettingsSource`（非官方 `YamlConfigSettingsSource`），`model_config` 里**不能**写 `yaml_file` / `yaml_file_encoding`。已修复，勿回退。

### 5.2 LLM 层 `src/secpt/core/llm`

| 文件 | 职责 |
|---|---|
| `base.py` | `LLMMessage`（role / content / tool_calls）、`BaseLLM`（ABC，`acompletion`）、`LLMError` |
| `litellm_adapter.py` | `LiteLLMAdapter`：`litellm.acompletion`，`model = f"{provider}/{model}"`，异常归一化为 `LLMError` |
| `fake.py` | `FakeLLM`：按 script 顺序回放（耗尽循环末条 + `RuntimeWarning`），或按 generator 动态产出 |
| `factory.py` | `get_llm(config)`：`fake=True` → `FakeLLM`，否则 `LiteLLMAdapter` |

**FakeLLM 的意义**：让所有下游模块（工具、runner、后续 Evaluator）能**零网络、零费用、确定性**地测试；真模型只出现在受守卫的冒烟用例里。这是整套测试策略的地基。

### 5.3 执行边界 `src/secpt/runtime/subprocess.py`

唯一允许触碰真实命令的地方。

```python
class SubprocessError(Exception): ...
class WhitelistError(SubprocessError): ...
class TimeoutExpired(SubprocessError): ...

@dataclass
class ExecResult:
    command: str; args: list[str]; returncode: int
    stdout: str; stderr: str; duration_ms: int; audit_id: str

async def run_whitelisted(cmd, args, *, config=None, timeout=None, audit_id=None) -> ExecResult: ...
def run_whitelisted_sync(...) -> ExecResult: ...   # CLI 用，内部 asyncio.run
```

已验证要点：**命令按基名匹配白名单**（`/bin/rm` 同样被拦）｜**严禁 `shell=True`**｜超时 `wait_for` + `kill` + `await proc.wait()` 防僵尸｜structlog 审计含 `shlex.join` 渲染、`whitelist_hit`｜**自检用 AST 而非文本扫描**避免 docstring 误报。

### 5.4 工具层 `src/secpt/core/tools`

```python
class Tool(ABC):
    name: str
    description: str
    @abstractmethod
    async def run(self, args: str) -> str: ...

class Registry:
    register(tool) / get(name) / all() / names()
```

- `demo.py` 提供 `EchoTool`（name=`echo`）走 `run_whitelisted`；真实侦察工具落在 `src/secpt/tools/recon/` 与 `tools/web/`；
- **异常吞噬**：工具异常转 `"Error: ..."` 字符串，runner 外层再兜一层（双保险）。

> 📌 Phase 2 待补：`Tool.run` 的**异常层级**（`ToolTimeout` vs `ToolWhitelistDenied`），让 runner 能区分「可重试」与「别再试」。

### 5.5 ReAct 循环 `src/secpt/core/react`

协议：LLM 每轮只输出 `ACTION: <tool> <args>` 或 `ANSWER: <text>`。

**双轨解析**：① 优先读 `resp.tool_calls`（结构化）；② 兜底正则解析文本。将来切结构化 tool_calls 时 runner 不用改。

```python
@dataclass
class ReactResult:
    answer: str; steps: int; exhausted: bool; trajectory: list[dict]

async def run_react(*, llm, tools, task, system_prompt=DEFAULT_SYSTEM_PROMPT,
                    max_steps=8, store=None) -> ReactResult
```

- 停止条件：命中 `ANSWER` 结束；耗尽 `max_steps` → `exhausted=True`；
- 自我纠正：未知工具/格式错/工具异常 → `observation="Error: ..."` 回灌；
- trajectory 事件：`action` / `format_error` / `finish` / `exhausted`；
- 向后兼容：`store` 等新参数默认 `None`。

**真实 trajectory 实录**：

```
step 1: action  tool=echo  args=hello  observation=hello
step 2: finish  answer='已用 echo 输出 hello'
ANSWER: 已用 echo 输出 hello        exhausted: False  steps: 2
```

DeepSeek 对 few-shot 文本协议遵循度良好，暂缓升级结构化 tool_calls。

### 5.6 世界模型 `src/secpt/core/graph`（🚧）

```python
class GraphStore(ABC):
    async def upsert_entity(eid, etype, props=None) -> None
    async def add_edge(src, dst, etype, confidence=None) -> None
    async def add_evidence(eid, payload, trust_tier="deterministic") -> str
    async def neighbors(eid) -> list[dict]
    async def close() -> None
```

`MemoryGraphStore`（dict 实现）+ `get_graph_store(cfg)` 工厂。当前 runner 把它当**记录 sink**（每步落 action 实体 / `performed` 边 / evidence），**尚不参与调度**——参与调度要等 Phase 2 的 Scheduler。

---

## 6. 能力主线：从单链到「面」★ Phase 2 目标架构

> 这一章是 v0.3 的重点。当前实现是**一条单链**（§5.5），本章定义它如何长成一张会自己收缩的图。

### 6.1 运行时总览

```
Genome（不可变：目标、范围、预算、禁止红线）
  └─ Node 树（同构分形，任意深度展开）
       ├─ 每个 Node 跑「六拍循环」，拥有自己的 ReAct 实例
       ├─ Scheduler 按 Wavefront 从就绪集挑一批 → asyncio.gather 并行
       ├─ Evaluator 给每个 Node 打分：Score = (价值 × 证据强度) / 成本
       ├─ 所有 Node 共享一处 WorldModel（图黑板）
       └─ DeathCertificate / BudgetReallocation / Degradation 写回 → 驱动下一次波前
```

关键区别于竞品的：**并行不是为了快，是为了让「收缩」有意义**——先铺开面取得多路证据，再用证据互相印证/证伪，把预算挤到活路上。

### 6.2 Genome（任务基因组）

不可变，Node 树的根约束。

```python
@dataclass(frozen=True)
class Genome:
    engagement_id: str
    targets: tuple[str, ...]        # 目标资产（主机/域名/网段）
    objective: str                  # 任务目标陈述
    budget_total: float             # 总预算（步数/时间/金额抽象单位）
    budget_per_node_cap: float
    max_depth: int                  # 分形树最大深度
    max_nodes: int                  # 同时存活节点上限
    forbidden: frozenset[str]       # 硬禁止（如 destructive 工具、禁止的目标）
```

> 📌 `forbidden` 是唯一现在就要在场的安全字段（配合 §7.3 白名单），它与「完整的 Scope 授权模型」不同——后者见 §8。

### 6.3 Node 与六拍循环

```python
@dataclass
class Node:
    id: str
    parent_id: str | None
    depth: int
    goal: str                       # 本节点要证明/证伪的目标
    status: Literal["ready","running","degraded","dead","done","waiting_parent"]
    budget: float
    evidence_ids: list[str]
    checkpoint_id: str | None
```

每个 Node 一轮**六拍**：

| 拍 | 动作 | 产出 |
|---|---|---|
| 1 感知 | 读 WorldModel 中与本 goal 相关的实体/边/他人结论 | 上下文视图 |
| 2 假设 | LLM 生成候选 `Hypothesis`（**不得直接写字实**） | Hypothesis |
| 3 行动 | 选定一个工具动作（受白名单与预算约束） | InFlightAction |
| 4 观察 | 执行并取回结构化输出 | Evidence(deterministic) |
| 5 评估 | Evaluator 打分：是否推进了 goal？是否需要衍生子节点？ | Score / 决策 |
| 6 记账 | **带 evidence_id 写图**；不可带证据写入的事实一律拒绝 | 图变更 |

**衍生规则**：第 5 拍若判定「当前发现打开了新方向」，则衍生子 Node（深度 +1，预算从父切分）。这是「一个变多个」的唯一入口，且必须经过 Evaluator，杜绝无限分裂。

### 6.4 Scheduler 与 Wavefront

每一轮从所有 `ready` 节点中挑一批组成**波前**，用 `asyncio.gather` 并行推进，受以下约束：

- **并发令牌**：`BudgetConfig.max_parallel`（默认建议 4~8，防把目标打挂、防 API 限流）；
- **挑选策略**：按 Evaluator 分数降序 + 深度优先/广度优先混合（建议：depth 浅者优先，避免一条线钻太深而上表面没铺开）；
- **预算扣除**：每拍扣减，归零则转为 `degraded` 并等待唤醒或复活；
- **父节点唤醒**：子节点产出关键证据（新凭证、新可达网段、高危漏洞验证成功）→ 把 `parent.status` 置回 `ready` 并触发紧急重排。

```python
async def step_wavefront(genome, nodes, store, evaluator, llm) -> list[NodeEvent]:
    ready = [n for n in nodes if n.status == "ready"]
    batch = evaluator.rank(ready)[: genome.max_nodes]
    results = await asyncio.gather(*[run_node(n, ...) for n in batch])
    return apply_events(results, nodes, store)
```

### 6.5 Evaluator 与预算分配

```
Score(node) = (Value × EvidenceStrength) / Cost
```

| 因子 | 计算 |
|---|---|
| `Value` | 该节点若证实，对 `Genome.objective` 的贡献（LLM 判定，走 `eval_model` R1，输出必须带理由） |
| `EvidenceStrength` | `max(已有证据 trust_tier 数值)`；无证据则为最低值 0.5（LLM 推理上限） |
| `Cost` | 已消耗预算 × 衰减因子；重复/失败动作者惩罚放大 |

**三条硬规则**：
1. LLM 只负责给 `Value` 打分并附理由，**预算的最终裁决权在系统手里**（原则 6）；
2. 分数低于阈值或连续 N 拍无新增实体 → 触发枯竭检测（§6.9）；
3. 被 Case ③ 降级的节点**保留 costing=∞ 但状态仍为 `degraded`**，不删除、可回收（§1.2 护栏）。

### 6.6 WorldModel 图黑板 Schema

| 模型 | 关键字段 | 说明 |
|---|---|---|
| `Entity` | `id`, `type`, `props`, `evidence_ids` | 主机/域名/端口/服务/组件/凭证/漏洞……类型可扩展，但**必须带 evidence_ids** |
| `Edge` | `src`, `dst`, `type`, `confidence`, `verified`, `evidence_ids` | `confidence = min(关联证据 trust)`；含推理成分的边 **封顶 0.5 且 `verified=False`** |
| `Evidence` | `id`, `payload`, `trust_tier`, `source`, `ts` | 一切事实的原子来源；删除需级联检查引用者 |
| `Hypothesis` | `id`, `stmt`, `status`, `confidence`, `parent_node` | 未经证实的 LLM 产物，status ∈ {open, confirmed, refuted, superseded} |
| `DeathCertificate` | `node_id`, `reason`, `evidence_ids`, `ts` | 机制 ① 产出，写入全局排除清单 |
| `InFlightAction` | `id`, `node_id`, `tool`, `args`, `checkpoint_id` | 推测执行追踪与回滚依据 |

**证据纪律的实现位置**：在 GraphStore 的写路径上做门控——`upsert_entity` / `add_edge` 若未提供至少一个 `evidence_id`，直接拒绝并抛 `MissingEvidence`。这是把原则 1 变成不可绕过的代码约束，而不是靠 prompt 自觉。

### 6.7 死亡证明与全局排除清单

节点进入 `dead` 必须开具死亡证明，理由分类：

| reason | 含义 | 是否可逆 |
|---|---|---|
| `no_live_targets` | 目标不可达/无开放端口 | 不可逆（写排除清单） |
| `tool_failed_permanently` | 工具缺失或被白名单永久拒绝 | 不可逆 |
| `hypothesis_refuted` | 核心假设被证伪（证据充分） | 不可逆 |
| `budget_exhausted` | 预算耗尽 | **可复活**（有新证据到达时可唤醒） |
| `superseded_by_sibling` | 兄弟节点已给出更强的路径 | 不可逆 |

**排除清单（ExclusionList）**是全局的：`{(target, direction) → DeathCertificate}`。任何新衍生节点在执行前先查它——命中则**不衍生**或**直接 dead**，从源头省掉预算。这就是机制 ① 让兄弟自动绕开的实现。

### 6.8 面收缩三机制的落地

```python
def get_narrowing_signals(engagement_id: str) -> NarrowingSignals:
    return NarrowingSignals(
        excluding=[...],    # 新增的死亡证明 → 写 ExclusionList
        wakeable=[...],     # 持有关键证据、父节点应被唤醒
        degraded=[...],     # 被机制③降级但可复活
    )
```

Scheduler 每轮开始前调用，据此调整波前组成。**这是三机制唯一的系统级入口**——避免把「收缩」散落在各 Node 里各凭自觉。

| 机制 | 触发判定 | 系统动作 |
|---|---|---|
| ① | Node 状态转 `dead` | 开 `DeathCertificate` → 写 `ExclusionList` → 兄弟/后续节点读清单绕开 |
| ② | Node 产出 **critical evidence**（凭证 / 可达新网段 / 可 RCE 已验证） | 标记父 `wakeable` → 父重新 allocation，从低分兄弟抽预算给新衍生 Node |
| ③ | Evaluator 判定当前特征与某条渗透经验高度匹配 | **降级**其他同级节点（不清数、不删除），并把它们的预算池转移给该路径；匹配度回落或假设被证伪 → 复活 |

### 6.9 反循环与枯竭检测

「这个方向已经没油了」无法直接观测，用**三个代理指标**联合判定：

| 指标 | 计算 | 枯竭征兆 |
|---|---|---|
| 单位预算新增图元素 | `Δ(entities+edges) / Δbudget` | 持续趋近 0 |
| 假设关闭率 | `Δ(closed hypotheses) / Δsteps` | 长时间为 0 |
| 响应新奇度衰减 | 本轮工具输出与历史输出的相似度滑窗 | 相似度持续 > 阈值 |

判定为枯竭后：先 `degraded`（可复活），连续多轮无改善才开死亡证明。

**另外必须有显式反循环**（不依赖 LLM 自觉）：同一 `(node, tool, normalized_args)` 重复执行 → 直接返回缓存结果并提示「已执行过」，防止 LLM 绕圈子式的死循环。

### 6.10 推测执行与 Checkpoint

- 允许 Node 基于 `Hypothesis` 抢跑（例如在 CVE 匹配度高时提前准备利用路径）；
- 每次推测前记录 `checkpoint_id`（图状态的轻量快照 / 或写变更日志用于回放）；
- 假设被 `refuted` → **回滚**该 checkpoint 之后本分支的所有图变更，并给相关分支发 `superseded` 信号；
- 回滚必须连带清理 `Entity/Edge` 上的 `evidence_ids` 引用，防悬空。

### 6.11 工具矩阵（渗透杀伤链映射）★ Phase 3 主要工作量

每一样都必须登记白名单、限定超时、返回结构化输出。**工具决定了电车的上界**——再多再好的机制，工具只有 echo 也白搭。

| 阶段 | 代表工具 | 建议 `Tool.name` | trust_tier | 备注 |
|---|---|---|---|---|
| 资产发现 | `whois` `dig` `subfinder` `amass` `httpx` | `recon_whois` `recon_dns` `recon_subenum` `web_probe` | deterministic | 输出量大，需截断/摘要 |
| 端口与服务 | `nmap` `masscan` | `recon_portscan` `recon_fastscan` | deterministic | 必须限制速率参数 |
| Web 指纹/内容 | `whatweb` `curl` `nuclei`(tech-detect) `gobuster` `ffuf` | `web_fingerprint` `web_fetch` `web_dirscan` | deterministic | 目录扫描注意并发 |
| 漏洞验证 | `nuclei` `nikto` `sqlmap` | `vuln_scan` `vuln_check` | deterministic / heuristic | 真正报告的维重点 |
| 凭据利用 | `hydra` `crackmapexec` | `cred_bruteforce` | deterministic | ⚠️ 属破坏性，见 §8 分级 |
| 后利用/域内 | `impacket-*` `linpeas` `BloodHound` | `post_*` | deterministic | 触发机制②的典型来源 |
| 报告 | 自研 | `report_build` | n/a | Phase 3 |

> 📌 **输出治理**：真实工具输出动辄几 MB，进 LLM 前必须**裁剪+结构化**（每个 Tool 自己负责，返回结构化摘要而非原始 stdout）—— 否则 token 爆炸，且把噪声证据污染进图。

### 6.12 端到端推演实例

目标 `example.com`，Root Node 展开波前一批三个子节点：

| 波前 | Node | 事件 |
|---|---|---|
| 1 | N1 端口服务 / N2 子域枚举 / N3 Web 指纹 | 三路并行开跑（真正意义的「面」） |
| 2 | N3 识别出 **Apache 2.4.49** | 命中 CVE-2021-41773 经验 → **机制③**：N1/N2 降级（可复活），预算转给 N3 深挖 |
| 3 | N2 子域枚举结论：全部 park 域名无指向 | **机制①**：开死亡证明 `no_live_targets` → ExclusionList 写入「子域方向」→ 后续新节点不再碰子域 |
| 4 | N3 拿到立足点/凭证 | 产出 **critical evidence** → **机制②**：唤醒 Root → Root 重排预算，衍生 N4「顺凭证做后利用」，把剩余预算抽给它 |
| 5 | N4 成功；N3 的假设被 confirming | Hypothesis `confirmed` → 结论带 evidence 写图；Root 收束生成报告骨架 |

这就是用户设想的那句话的实现形态：**先撒开面，再用证据把面挤成一条活路。**

---

## 7. 硬约束红线（现在就必须守，不属于可后置治理）

### 7.1 密钥绝不落盘
- `api_key_env` 语义是**环境变量名**，不是 key 本身；
- 真实密钥只走 `export DEEPSEEK_API_KEY=...`，**禁止**进 `config.yaml` / 提交版 `.env` / 代码。
> 📌 曾发生把 `sk-...` 写进 `config.yaml` L14 的事故，已修正。再出现即为回归缺陷。

### 7.2 DeepSeek 报文约束
- 无真实 `tool_calls` 时报文中**不接受 `role="tool"`**；
- observation 统一以 **`role="user"` + `观察：` 前缀**回发；
- adapter 序列化时给 `role="tool"` 补 `tool_call_id: "call_placeholder"` 做防御。

### 7.3 命令执行安全
- 白名单按**命令基名**匹配（`/bin/rm` 一样拦）；
- **禁止 `shell=True`**；shell 串只用于日志渲染（`shlex.join`）；
- 超时必须 `kill` + `wait`；
- 每次执行必须 structlog 审计（含 `whitelist_hit`）。

### 7.4 信任分级与传播
| 来源 | `trust_tier` | 数值 |
|---|---|---|
| 工具确定性执行输出 | `deterministic` | 1.0 |
| 启发式/规则推断 | `heuristic` | 0.8 |
| LLM 纯推理结论 | `llm_inference` | 0.5 |

传播规则：`Edge.confidence = min(关联证据 trust_tier)`；含推理成分的边**封顶 0.5 且 `verified=False`**。

### 7.5 测试策略
- **FakeLLM 先行**：先有确定性单测，再谈真模型；
- **真模型受守卫**：`@pytest.mark.skipif(not os.getenv("DEEPSEEK_API_KEY"), ...)`，**无 key 时 CI 必须干净**；
- **向后兼容回归**：新增可选参数默认值必须让旧用例零改动全过；
- FakeLLM script 耗尽的 `RuntimeWarning` 属预期行为，不计为缺陷。

---

## 8. 治理债台账 ★ 有意后置，非遗忘

> 依 §1.3「千里马原则」主动延后。每项写明**触发条件**——条件达成时必须立即补做。

| # | 治理项 | 风险 | 触发条件 | 前置成本 |
|---|---|---|---|---|
| G1 | **目标授权范围（Scope）** | 🔴 高：白名单管命令不管目标，无机制阻止对任意目标执行 | **对外交付前 / 接客户目标前 / 多任务并发前** | 低（执行边界前加一层目标校验） |
| G2 | 速率限制 | 🟠 中：可能打挂目标、触发对方 IPS | 接入真实目标实操时 | 低（并发令牌已有雏形） |
| G3 | 高危动作人工确认 | 🟠 中：如 hydra 爆破、破坏性 sqlmap 参数 | 引入该类工具时即需 | 低（Tool 调用前插闸门） |
| G4 | 破坏性动作分级 | 🟠 中：无法区分只读 vs 写操作 | 同 G3 | 中（需给每个 Tool 标注 risk level） |
| G5 | 审计持久化 | 🟡 低~中：目前只有 structlog，进程外无处审计 | **交付 / 合规审查前** | 低（`audit/` 目录已预留） |
| G6 | Node 树递归爆炸保护 | 🟡 中： max_depth/max_nodes 已有配置，但需实测 | Phase 2 首次多波前跑通时 | 低 |

> ⚠️ **唯一例外**：G6 属能力本身的一部分，Phase 2 实现 Node 树时**必须同时做**，不属于可后置。

---

## 9. 测试基线与验收指标

### 9.1 现有基线（必须保持）

```bash
cd /home/kali/secpt

pytest -q                                    # 基线：26 passed, 3 skipped
pytest tests/unit/core/test_llm.py -q
pytest tests/unit/runtime/test_subprocess.py -q
pytest tests/unit/core/test_react.py tests/unit/core/test_tools.py -q

# 真实模型冒烟（需 key）
export DEEPSEEK_API_KEY="..."                # 安全来源取，勿写进仓库/聊天
export SECPT_TOOLS__WHITELIST='["echo"]'
pytest tests/unit/core/test_deepseek_smoke.py -v
```

### 9.2 能力主线的量化验收（目标值待校准）

Phase 2 落地后应有下面这类指标，作为「是不是真的在收缩」的证据：

| 指标 | 定义 | 目标（待校准） |
|---|---|---|
| 证据合规率 | 图上 `Entity/Edge` 携带 `evidence_id` 的比例 | **100%**（不得有一例外） |
| 重复动作率 | 同一 `(node, tool, args)` 重复执行占比 | < 5% |
| 面收缩有效性 | 被 ExclusionList 拦下的计划动作数 / 总计划动作数 | > 20% |
| 关键证据唤醒延迟 | 子节点产出关键证据 → 父节点完成预算重排的波前数 | ≤ 1 |
| 首次关键路径步数 | 从任务开始到出现第 1 个 critical evidence 的平均步数 | 越低越好（需与基线对比） |
| 图增长效率 | `Δ(entities+edges) / Δbudget` | 高且稳定（枯竭检测的输入） |

> 📌 不要只看「有没有跑完」——这系统的价值全在「有没有靠证据收缩」。指标没做出来之前，Phase 2 不算完成。

---

## 10. 路线图（能力优先重排）

```
Phase 0   脚手架 / 技术选型                                    ✅
Phase 1   最小可运行骨架（先能跑）
    A  LLM 适配层 litellm + FakeLLM + 工厂                    ✅
    B  白名单 subprocess 执行边界                              ✅
    C  Tool 抽象 + 最小 ReAct 循环                             ✅
    3b 真实 DeepSeek 打通（few-shot + 冒烟）                   ✅
    4a GraphStore 抽象 + 内存后端 + 接进 runner                 ✅
    4b SQLite 后端（SQLAlchemy async + aiosqlite）             ✅
    4c Postgres 后端（asyncpg）                                ✅
Phase 2   ★ 能力主线：从单链到「面」（跑好）
    2.1 Node + Genome + 六拍循环（单节点多波前串行）            ✅
    2.2 Scheduler + Wavefront 真正并行（含 G6 递归保护）        ✅
    2.3 WorldModel 证据纪律（MissingEvidence 门控）+ 信任传播    ✅
    2.4 Evaluator + 预算分配                                   ✅
    2.5 死亡证明 + ExclusionList（机制①）                       ✅
    2.6 关键证据唤醒父节点 + 预算重排（机制②）                  ✅
    2.7 经验匹配降级 + 可复活（机制③）                          ✅
    2.8 反循环 / 枯竭检测（三代理指标）                         ✅
    2.9 推测执行 + Checkpoint 回滚                             ✅
Phase 3   让它真的能打（跑精）
    3.1 真实工具矩阵（§6.11，杀伤链全覆盖）                     ✅
    3.2 输出裁剪与结构化（防 token 爆炸）                       ✅
    3.3 策展知识库 / 经验库                                     ✅
    3.4 报告生成                                               ✅
Phase 4   治理补做（依 §8 触发条件）
    G1 Scope 授权 / G2 限速 / G3 高危确认 / G4 破坏性分级 / G5 审计 ✅
```

**方法论约定**：一切按「**一个功能一个提示词、跑绿再进下一个**」推进；每步验收 = pytest 输出 +（涉真模型时）真实 trajectory；能力项还需给出 §9.2 的对应指标。

---

## 11. 目录结构

```
/home/kali/secpt
├── pyproject.toml            # 依赖 + [project.scripts] secpt
├── config.yaml               # ⚠️ 禁止出现任何密钥
├── .env.example              # 空壳，真实 .env 已 gitignore
├── src/secpt/
│   ├── cli/app.py            # typer CLI
│   ├── config/
│   │   ├── settings.py       # pydantic-settings + 自定义 YamlConfigSource
│   │   └── models.py         # LLMConfig / ToolsConfig / GraphConfig
│   ├── core/
│   │   ├── llm/              # ✅ base / litellm_adapter / fake / factory
│   │   ├── react/            # ✅ prompts / runner
│   │   ├── tools/            # ✅ base / demo        ⬜ 真实工具实现
│   │   ├── graph/            # 🚧 base / memory / factory
│   │   ├── node/             # ⬜ Genome / Node / 六拍循环
│   │   ├── scheduler/        # ⬜ Wavefront / 并发令牌
│   │   └── evaluator/        # ⬜ 打分 / 枯竭检测 / narrowing signals
│   ├── runtime/
│   │   ├── subprocess.py     # ✅ 白名单执行边界
│   │   └── runner.py
│   ├── tools/{recon,web}/    # ⬜ 侦察/Web 工具实现
│   ├── skills/               # ⬜ 经验库（渗透知识策展）
│   ├── telemetry/logging.py  # structlog 配置
│   └── audit/                # ⬜ 审计持久化（G5）
└── tests/
    ├── conftest.py
    ├── unit/                 # ✅ llm / subprocess / react / tools / (graph 🚧)
    └── integration/          # ⬜ 多波前端到端
```

---

## 12. 附录

**术语表**

| 术语 | 含义 |
|---|---|
| 面收缩 | 用多路证据互相印证/证伪，把并行探索的范围逐步挤到少数活路上的过程 |
| 波前（Wavefront） | 一轮并行推进的一组就绪节点 |
| 死亡证明 | 节点判定为不可逆死路时开具的记录，写入全局排除清单 |
| 证据纪律 | 图上任何结论必须挂载 evidence_id，否则拒绝写入 |
| 千里马原则 | §1.3，能力优先、治理后置的产品决策 |
| 治理债 | §8，有意延后但记了账、写了触发条件的治理想 |

**一句话总结当前定位**：今天手里是一条**能跑、能接真模型的最小链路**；下一步要做的，是让这条链长成分形的树、并列成波前、再靠证据把它自己挤成一条活路。