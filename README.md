# Agent Harness - 通用智能体运行框架

> **归档说明（2026-10-28）**：本仓库已转向渗透测试智能体自研骨架（见 [docs/pentest-agent-development-doc-v0.1.md](docs/pentest-agent-development-doc-v0.1.md)）。
> 下文描述的通用 harness 代码已从磁盘移除，完整内容保留在 git 历史（commit `0d0ffd0` 及之前），仅供查阅参照；本 README 亦为归档文档，与新骨架不再对表。

一个模块化的 AI 智能体 harness（运行框架/载体），提供运行引擎、会话管理、工具协议、适配器层和插件系统，用于构建各种具体智能体应用。

## 快速开始

```bash
# 安装依赖
pip install -e ".[dev]"

# 配置环境变量
cp .env.example .env
# 编辑 .env，填入 API Key（LLM_PROVIDER=mock 可离线演示，无需 Key）

# 统一入口：secpt <命令>（等价 python -m app.main <命令>）
secpt doctor                 # 环境自检
secpt cli                    # 交互式终端（single/team/swarm 三种模式）
secpt web [--host --port]    # Web 界面（SSE 流式对话 + 运行回放 + 配置中心）
```

Web 与 CLI 共享同一份构造逻辑（`app/bootstrap.py`）和配置文件
（`configs/skills/*.md`、`configs/mcp.json`），任一入口都能管理配置：

```bash
secpt tools                        # 列出全部工具（含危险标记）
secpt skills list|show|test        # 技能管理（与 Web 配置中心同源）
secpt mcp list|connect|call        # MCP 服务器管理（连接测试 / 工具调用）
```

## CLI 交互命令

| 命令 | 说明 |
|------|------|
| `quit` / `exit` / `q` | 退出会话 |
| `mode single` | 切换到单 Agent 模式（ReAct 循环） |
| `mode team` | 切换到多 Agent 协作模式（Router 路由） |
| `mode swarm` | 切换到事件驱动集群模式（Planner 拆意图 → 多 Worker 并行认领，黑板共享发现） |
| `tools` | 查看可用工具 |

## 目录结构

```
secpt/
├── harness/            # 核心包：配置、日志、记忆、韧性等横切能力
│   ├── memory/         # 短期对话 + 长期存储 + 冷节点压缩（InMemory / SQLite）
│   ├── persistence/    # 状态持久化：快照存储 + 检查点管理
│   ├── planner/        # 任务拆解为可执行步骤（LLM / 模板）
│   ├── evaluator/      # 结果评分和反馈
│   ├── skills/         # 可复用技能（提示词技能 / 工具技能）+ 注册表
│   ├── resilience/     # LLM 韧性：重试、熔断、限流、响应缓存
│   ├── tools/          # 工具体系：注册表、@tool 装饰器、内置工具集、脚本加载器
│   ├── security/       # 人机协同审批（HITL）
│   ├── observability/  # 执行链路追踪（Span / Trace）
│   ├── config/         # 配置管理
│   ├── logging/        # structlog 结构化日志
│   └── utils/          # 工具函数
├── runtime/            # 运行时：执行循环、黑板、事件驱动、多智能体协作
│   ├── blackboard/     # 黑板体系：base 基础 / hierarchical 分层 / graph 图状态
│   ├── executor/       # 计划执行器
│   ├── agents/         # 子智能体 + 协作模式（Router/Parallel/Debate）+ 角色配置
│   ├── loop.py         # ExecutionLoop：ReAct 循环引擎
│   ├── event_driven.py # 事件驱动 Planner + Frontier 意图队列 + Worker
│   ├── intervention.py # 人机协作：steer_work / side_questions
│   ├── streaming.py    # 流式输出（SSE / WebSocket）
│   ├── distributed.py  # 分布式执行（多节点协作）
│   ├── state.py        # 有限状态机
│   └── events.py       # 事件总线
├── adapters/           # 适配器：LLM 提供商、工具协议（MCP）
│   ├── llm/            # 统一接入 OpenAI / DeepSeek / Anthropic
│   └── mcp/            # Model Context Protocol 客户端
├── plugins/            # 插件系统
│   ├── marketplace.py  # 插件市场：发现、加载、生命周期管理
│   └── prompts/        # 提示词模板库
├── app/                # 示例应用：展示如何用 harness 构建具体智能体
│   └── cli/            # 交互式 CLI
├── configs/            # 声明式配置：roles.json（角色定义）
└── tests/              # 测试：unit、e2e
```

## 架构分层

```
┌─────────────────────────────────────┐
│        SubAgent (业务层)              │
│  - 角色定义 (instructions)           │
│  - 工具集 (tools)                   │
│  - 协作关系 (Router/Parallel/Debate) │
└──────────────┬──────────────────────┘
               │ 使用
               ▼
┌─────────────────────────────────────┐
│        ExecutionLoop (引擎层)         │
│  - ReAct 循环实现                    │
│  - 工具调用调度                      │
│  - 步数/超时控制                     │
│  - HITL 人工确认                     │
│  - 事件触发                         │
└──────────────┬──────────────────────┘
               │ 调用
               ▼
┌─────────────────────────────────────┐
│          LLMClient (适配层)           │
│  - OpenAI / DeepSeek / Anthropic    │
│  - 统一 chat() 协议                  │
└─────────────────────────────────────┘
```

## 核心模块

| 模块 | 职责 |
|------|------|
| `runtime.loop` | ExecutionLoop：ReAct 循环引擎（Thought → Action → Observation） |
| `runtime.state` | 有限状态机：IDLE → PLANNING → EXECUTING → COMPLETED |
| `runtime.events` | 事件总线：同步/异步监听、一次性订阅 |
| `runtime.agents` | SubAgent + 协作模式（Router/Parallel/Debate） |
| `harness.memory` | 记忆存储（InMemory / SQLite） |
| `harness.planner` | 任务规划（LLM 生成 / 模板匹配） |
| `harness.tools` | 工具注册（带 Schema、异步支持） |
| `harness.skills` | 技能模块（PromptSkill / ToolSkill / SkillRegistry） |
| `harness.resilience` | LLM 韧性（重试 / 熔断 / 限流 / 缓存） |
| `harness.observability` | 执行链路追踪（Span / Trace） |
| `harness.security` | 权限控制（HITL 人工确认） |
| `adapters.llm` | LLM 适配器（OpenAI / DeepSeek / Anthropic） |
| `adapters.mcp` | MCP 协议客户端 |

## SubAgent 使用方式

```python
from runtime.agents.base import SubAgent

# 方式1: run() - 走 ExecutionLoop，支持 ReAct 循环 + 工具调用
result = await agent.run("分析这段代码", context="...")
# 内部: ExecutionLoop.run_react() → Thought → Action → Observation

# 方式2: run_simple() - 单次 LLM 调用，不走 ExecutionLoop
result = await agent.run_simple("写一首诗")
# 内部: 直接 llm.chat()，适合简单任务
```

## 协作模式

| 模式 | 类名 | 适用场景 |
|------|------|----------|
| Router | `Router` | 任务路由到专家 Agent |
| Parallel | `Parallel` | 多个 Agent 并行执行，结果汇总 |
| Debate | `Debate` | 正反方辩论，Judge 裁决 |
| BlackboardRouter | `BlackboardRouter` | 带共享黑板的路由模式 |
| BlackboardParallel | `BlackboardParallel` | 带共享黑板的并行模式 |

## 角色配置

角色三要素（提示词 / 工具集 / 步数上限）收敛到 `RoleSpec`，支持三种来源：
**内置模板库**、**JSON 配置文件**（可覆盖内置）、**代码注册**。

### 内置模板 + 工厂构造

```python
from runtime.agents.roles import RoleRegistry

roles = RoleRegistry.with_builtins()
# 内置角色: planner / coordinator / researcher / coder / reviewer / writer / analyst

# 按角色声明工厂化构造 SubAgent，每个角色拿到声明的最小工具集
coder = roles.create_agent("coder", llm, shared_tools=tools)
writer = roles.create_agent("writer", llm)          # 声明无工具 → 纯文本角色
```

### JSON 配置文件

在 `configs/roles.json` 中声明自定义角色或覆盖内置（CLI team 模式自动加载）：

```json
{
  "roles": {
    "pentest_recon": {
      "instructions": "你是渗透测试信息收集专家...",
      "tools": ["http_request", "read_file"],
      "description": "安全侦察角色"
    }
  }
}
```

### 代码注册与查询

```python
from runtime.agents.roles import RoleSpec

roles.register(RoleSpec(name="qa", instructions="你是测试工程师...", tools=["run_command"]))
roles.list_roles()        # {name: description} 全量角色
roles.get("coder").tools  # 查看角色声明的工具名单
```

设计要点：
- **最小权限**：`create_agent` 按名单过滤共享工具池构造私有 ToolRegistry
- **可降级**：角色声明了不存在的工具 → 告警跳过，不中断构造
- **配置腐化防护**：非法配置（缺 instructions、坏 JSON）明确抛 `RoleError` 系列异常

## 共享黑板（Blackboard）

多智能体协同工作的共享状态机制。

### 基础黑板（`Blackboard`）

```python
from runtime.blackboard import Blackboard, BlackboardAgent

blackboard = Blackboard()

agent1 = BlackboardAgent(
    name="analyzer",
    instructions="分析数据",
    llm=llm,
    blackboard=blackboard,
    write_keys=["analysis"],
)

agent2 = BlackboardAgent(
    name="advisor",
    instructions="提供建议",
    llm=llm,
    blackboard=blackboard,
    read_keys=["analysis"],
    write_keys=["advice"],
)

# 并行执行，自动协同
parallel = BlackboardParallel(
    sub_agents=[agent1, agent2],
    blackboard=blackboard,
)
result = await parallel.execute("分析销售数据并给出建议")
```

### 分层黑板（`HierarchicalBlackboard`）

解决长时间、高复杂任务的上下文爆炸问题：

```
┌─────────────────────────────────────┐
│  Global Blackboard (全局摘要)        │  ← 关键结论、摘要
│  - 容量: 50 条                       │
│  - 单条: 5KB                         │
│  - TTL: 1 小时                       │
├─────────────────────────────────────┤
│  Domain Blackboard (领域分区)        │  ← 按模块/主题隔离
│  - 每个域容量: 200 条                │
│  - 单条: 10KB                       │
│  - TTL: 30 分钟                     │
├─────────────────────────────────────┤
│  Local Blackboard (Agent 私有)       │  ← 临时数据
│  - 每个 Agent 容量: 100 条           │
│  - 单条: 5KB                        │
│  - TTL: 5 分钟                      │
└─────────────────────────────────────┘
```

**特性**：
- **TTL 自动过期**：过期条目自动清理
- **容量限制**：防止内存耗尽
- **智能过滤**：只读取相关条目
- **乐观锁**：`expected_version` 防止竞态

```python
from runtime.blackboard import HierarchicalBlackboard

bb = HierarchicalBlackboard(
    global_max_entries=50,
    domain_max_entries=200,
    local_max_entries=100,
)

# 写入不同层
await bb.write_global("summary", "项目整体健康")
await bb.write_domain("network", "findings", "端口扫描结果")
await bb.write_local("agent_1", "temp_data", "临时计算结果", ttl=60)

# 智能读取：只获取相关条目
relevant = await bb.read_relevant(
    query_keywords=["network", "security"],
    limit=10,
)
```

### 关键信息保护

防止关键信息被 TTL 清理或容量驱逐：

```python
# 方式 1: 写入时标记为关键
await bb.write_critical(
    key="security_alert",
    value="发现 SQL 注入漏洞",
    agent="security_agent",
    reason="安全漏洞必须保留",
    domains=["frontend", "backend"],  # 可选：广播到其他领域
)

# 方式 2: 事后标记现有条目
await bb.pin_entry("global", "important_find")

# 获取所有关键信息
critical = await bb.get_critical_entries()
```

**关键信息特性**：
- 永不过期（`expire_at = 0`）
- 不受容量限制
- 清理时保留

### 跨领域广播

关键信息自动同步到相关领域：

```python
await bb.broadcast_to_domain(
    source_agent="network_agent",
    key="port_scan_alert",
    value="端口 8080 异常",
    reason="跨领域安全告警",
    target_domain="application",
)

# 目标领域收到结构化数据：
# {
#     "data": "端口 8080 异常",
#     "source": "network_agent",
#     "reason": "跨领域安全告警",
#     "timestamp": 1234567890
# }
```

### 冲突解决（乐观锁）

```python
entry = await bb.write_global("counter", 1)

# 基于版本更新
await bb.write_global("counter", 2, expected_version=entry.version)

# 如果版本不匹配（其他人已修改），抛出 ConflictError
```

## 工具体系

### 内置工具（开箱即用）

```python
from harness.tools.registry import ToolRegistry
from harness.tools.builtin import register_builtin

registry = ToolRegistry()
register_builtin(registry)          # 一键注册全部内置工具
# register_builtin(registry, include_dangerous=False)  # 只读场景排除危险工具
```

| 类别 | 工具 | 说明 |
|------|------|------|
| 基础 | `get_time` | 当前时间 |
| 基础 | `calculate` | 安全计算器（AST 白名单求值，**非 eval**） |
| 文件 | `read_file` / `write_file` / `list_dir` / `search_text` | 读写文件、列目录、正则搜索 |
| 命令 | `run_command` | argv 列表执行（**无 shell 解释**，拒绝控制字符注入） |
| 命令 | `python_script` | 执行用户 Python 脚本 |
| 网络 | `http_request` | HTTP 请求（仅 http/https，响应截断） |

危险工具（`write_file`/`run_command`/`python_script`）标记 `dangerous=True`，
配合 HITL 控制器走人工确认。

### 自己写工具：@tool 装饰器

写一个普通函数（带类型注解和 docstring），Schema 自动生成：

```python
from harness.tools.decorator import tool

@tool(dangerous=False)
def get_weather(city: str, days: int = 3) -> str:
    """查询城市天气预报。

    Args:
        city: 城市名
        days: 预报天数
    """
    return f"{city} 未来 {days} 天晴"

registry.register_function(get_weather)
```

### 自己写脚本：脚本工具加载器

在项目根 `tools/` 目录下写普通 Python 文件即可，CLI 启动时自动加载：

```python
# tools/weather.py
TOOL_NAME = "weather"          # 可选，默认取文件名
TOOL_DESCRIPTION = "查询城市天气"  # 可选，默认取 execute 的 docstring 首行

def execute(city: str, days: int = 3):   # 同步/异步均可
    return f"{city} {days} 天晴"
```

加载行为：坏脚本只告警跳过不影响其他脚本；与已有工具重名时跳过不覆盖；
`@tool` 装饰过的函数同样识别。手动加载：

```python
from harness.tools.loader import ScriptToolLoader

loaded = ScriptToolLoader("tools").load_into(registry)
```

### 底层注册（手动 Schema）

```python
registry.register(
    name="delete_file",
    func=lambda path: f"deleted {path}",
    description="删除文件",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    dangerous=True,  # HITL 确认
)
```

## Skills 技能模块

```python
from harness.skills.base import PromptSkill, ToolSkill, SkillContext, SkillRegistry

# 提示词技能：基于模板和 LLM
skill = PromptSkill(
    name="summarize",
    description="文本摘要",
    template="请对以下内容进行摘要：\n\n{input}"
)

# 工具技能：组合多个工具
skill = ToolSkill(
    name="data_process",
    description="数据处理",
    workflow=[
        {"tool": "extract", "args": {"source": "{input}"}},
        {"tool": "transform", "args": {"data": "{extract_result}"}},
    ]
)
skill.register_tool("extract", lambda source: f"extracted({source})")

# 执行技能
llm = build_llm("deepseek")
context = SkillContext(llm=llm, variables={"extra": "变量"})
result = await skill.execute("输入文本", context)
```

## LLM 韧性

> **接线状态（2026-10-27 起已全部接入运行链路）**：
> - `build_llm()` 返回的客户端已自动包 `ResilientLLM`（重试/熔断/限流/缓存），CLI 全模式生效
> - 危险工具审批：`ToolRegistry.approval_hook` 由 CLI 自动挂载，`run_command`/`python_script`/`write_file` 执行前需终端 y/N 确认
> - 上下文预算：`ExecutionLoop` 默认 60K 字符预算，超限自动截断旧轮次观察（保留 system + 最近 3 轮完整）
> - 会话历史：single 模式跨轮记忆（上一轮 messages 传入下一轮 `history=`）
> - 黑板持久化：swarm 模式退出时快照到 `snapshots/blackboard.json`，下次启动自动恢复跨会话记忆

```python
from harness.resilience.llm import ResilientLLM, RetryConfig, CircuitBreakerConfig

# 创建高韧性 LLM 客户端
llm = ResilientLLM(
    base_client,
    retry_config=RetryConfig(max_retries=3, base_delay=1.0),
    breaker_config=CircuitBreakerConfig(failure_threshold=5, recovery_timeout=30),
    rate_limit_config=RateLimitConfig(max_requests=60, window_seconds=60),
    cache_ttl=3600,  # 缓存 1 小时
    enable_cache=True,
)

# 使用：自动重试、熔断保护、限流、缓存
response = await llm.chat(messages, tools)

# 查看统计
stats = llm.get_stats()
# {"total_requests": 100, "cache_hits": 30, "failures": 2, ...}
```

## 记忆存储

```python
from harness.memory.store import InMemoryMemory, SQLiteMemory, MemoryRecord

# 内存存储（开发）
memory = InMemoryMemory()

# SQLite 持久化（生产）
memory = SQLiteMemory("data/memory.db")

# 存储记忆
memory.remember(MemoryRecord(
    key="user_name",
    content="Alice",
    kind="fact",
    namespace="user_123"
))

# 检索
record = memory.recall("user_name", namespace="user_123")
```

## 任务规划

```python
from harness.planner.base import Planner, TemplatePlanner

# LLM 生成计划
planner = Planner(llm)
plan = await planner.create_plan("分析销售数据并生成报告")

# 模板匹配
planner = TemplatePlanner()
plan = planner.create_plan("审查代码", template="code_review")
```

## 可观测性

```python
from harness.observability.tracer import Tracer

tracer = Tracer(storage_path="data/traces")

# 开始追踪
trace_id = tracer.start_trace("用户任务")

# 记录操作
tracer.record_llm_call(tokens=100, model="gpt-4")
tracer.record_tool_call("search")

# 结束追踪
tracer.end_trace()

# 查看统计
summary = tracer.get_summary()
```

## 权限控制（HITL）

```python
from harness.security.hitl import HITLController, console_approval_callback

# 创建控制器
controller = HITLController(console_approval_callback)

# 请求审批
approved = await controller.request_approval("delete_file", '{"path": "/tmp/test"}')

# 在 ExecutionLoop 中使用
loop = ExecutionLoop(llm, tools, hitl_callback=controller.request_approval)
```

## 配置

环境变量（见 `.env.example`）：

| 变量 | 说明 |
|------|------|
| `LLM_PROVIDER` | 默认 LLM 提供商（deepseek/openai/anthropic/mock）。`mock` 为离线演示模式：无需 API Key 即可跑通流式输出、工具调用与 HITL 审批全链路 |
| `DEEPSEEK_API_KEY` | DeepSeek API Key |
| `OPENAI_API_KEY` | OpenAI API Key |
| `ANTHROPIC_API_KEY` | Anthropic API Key |
| `WEB_API_KEY` | Web 服务 API 认证密钥。非空时所有 `/api/*` 接口要求请求头 `X-Api-Key`，且服务绑定 `0.0.0.0` 允许远程访问；为空时服务仅绑定 `127.0.0.1` 本机访问（`/api/health` 与静态页除外） |
| `MEMORY_BACKEND` | 记忆存储后端（memory/sqlite） |
| `LOG_LEVEL` | 日志级别 |

Web 前端远程访问示例：

```bash
# 服务端 .env
WEB_API_KEY=my-secret-key

# 启动后前端首次请求会弹出密钥输入框；或直接在控制台预置：
localStorage.setItem('secpt_api_key', 'my-secret-key')
```

## 测试

```bash
# 运行所有测试
pytest tests/ -v

# 运行单元测试
pytest tests/unit/ -v

# 运行端到端测试
pytest tests/e2e/ -v
```

## 事件驱动架构（Event-Driven）

借鉴 ARTEX 设计，实现黑板变更 → Planner 唤醒 → 意图生成 → Worker 执行的完整事件驱动流程。

### 核心组件

```
┌─────────────────────────────────────────┐
│  Blackboard (黑板)                       │  ← 状态变更触发事件
│     ↓ on_write / on_delete              │
│  EventDrivenPlanner (事件驱动规划器)      │  ← Debounce 聚合唤醒
│     ↓ generate intents                  │
│  Frontier (意图队列)                     │  ← 优先级排序
│     ↓ claim                             │
│  Worker[] (执行器并发运行)                │  ← 竞争认领执行
│     ↓ complete / fail                   │
│  Blackboard (结果写回)                   │  ← 触发下一轮
└─────────────────────────────────────────┘
```

### 快速开始

```python
import asyncio
from runtime.blackboard import HierarchicalBlackboard
from runtime.event_driven import EventDrivenEngine

# 创建黑板
bb = HierarchicalBlackboard()

# 写入初始状态
await bb.write_global("task", "实现用户认证系统")
await bb.write_critical(
    key="security_req",
    value="必须使用 OAuth2",
    agent="architect",
    reason="安全合规要求"
)

# 创建引擎（2 个 Worker）
engine = EventDrivenEngine(bb, llm, num_workers=2, debounce_ms=300)

# 运行
result = await engine.run(max_rounds=5)
print(f"完成意图: {len(result['completed_intents'])}")
print(f"唤醒次数: {result['wake_count']}")
```

### Frontier 意图队列

```python
from runtime.event_driven import Frontier, Intent, Worker

frontier = Frontier()

# 提交意图（自动分配 ID）
intent = Intent(
    description="实现登录接口",
    agent_role="coder",
    priority=0,  # 高优先级
)
await frontier.submit(intent)

# Worker 竞争认领
worker = Worker("worker_1", llm)
claimed = await worker.run_once(frontier)
```

**特性**：
- **优先级排序**：高优先先执行（负数优先级实现）
- **原子认领**：`asyncio.Lock` 保证同一意图只被一个 Worker 认领
- **状态追踪**：PENDING → RUNNING → COMPLETED/FAILED

### 事件驱动 Planner

```python
from runtime.event_driven import EventDrivenPlanner

planner = EventDrivenPlanner(
    blackboard=bb,
    llm=llm,
    debounce_ms=500,        # 防抖：500ms 内多次变更只触发一次
    max_intents_per_wake=5,  # 每次唤醒最多生成 5 个意图
)

# 黑板变更自动触发（通过 subscribe）
# 或手动唤醒
intents = await planner.wake()
```

**工作原理**：
1. 黑板写入触发 `on_write` 事件
2. Debounce 聚合：500ms 内的多次变更只触发一次唤醒
3. Planner 分析黑板状态，调用 LLM 生成意图
4. 意图提交到 Frontier，等待 Worker 认领

### Worker 执行器

```python
from runtime.event_driven import Worker

# 创建 Worker
worker = Worker(
    worker_id="coder_1",
    llm=llm,
    tools={"code_review": review_func},  # 可选工具
)

# 执行一次认领-执行
result = await worker.run_once(frontier)

# 持续执行直到空闲（最多空闲 5 秒自动退出）
completed = await worker.run_loop(frontier, max_idle=5.0)
```

### 完整示例：多轮任务规划

```python
async def multi_round_task():
    bb = HierarchicalBlackboard()
    llm = create_llm()

    # 初始任务
    await bb.write_global("project", "开发电商系统")

    # 创建引擎
    engine = EventDrivenEngine(bb, llm, num_workers=3)

    # 第一轮：生成初始计划
    result = await engine.run(max_rounds=1)

    # 检查完成状态，决定是否需要更多轮次
    stats = result["frontier_stats"]
    if stats.pending > 0:
        # 继续执行
        result = await engine.run(max_rounds=5)

    return result
```

### 配置参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `debounce_ms` | 防抖毫秒数 | 500 |
| `max_intents_per_wake` | 每次唤醒最大意图数 | 5 |
| `num_workers` | Worker 数量 | 3 |
| `max_rounds` | 最大运行轮数 | 10 |

## 高级特性

### 图状态黑板（Graph Blackboard）

用图结构表达实体间复杂关系，支持路径分析和子图提取。

```python
from runtime.blackboard import GraphBlackboard, NodeType, EdgeType

gb = GraphBlackboard()

# 添加节点
api = gb.add_node(NodeType.ASSET, "API 服务", {"url": "https://api.example.com"})
vuln = gb.add_node(NodeType.FINDING, "SQL 注入", {"severity": "high"})
task = gb.add_node(NodeType.TASK, "修复漏洞")

# 建立关系
gb.add_edge(vuln.id, api.id, EdgeType.BELONGS_TO)
gb.add_edge(task.id, vuln.id, EdgeType.MITIGATES)

# 查询
neighbors = gb.get_neighbors(api.id)
subgraph = gb.get_subgraph(api.id, depth=2)
path = gb.find_path(vuln.id, task.id)
```

**优势**：
- 表达"漏洞 A 属于服务 B，任务 C 修复漏洞 A"
- 支持 transitive 查询（A 影响 B，B 影响 C → A 影响 C）
- 子图提取（只看某个模块的状态）

### 冷节点压缩（Cold Node Compaction）

长期未访问的记忆自动压缩为 LLM 生成的摘要，释放内存。

```python
from harness.memory.compactor import Compactor, SmartMemoryWithCompaction

# 方式 1: 独立压缩器
compactor = Compactor(llm, cold_threshold_seconds=3600)
compactor.record_access("memory_1")
# ... 1 小时后 ...
compactor.mark_cold_nodes()
digest = await compactor.compact_node("memory_1", original_content)

# 方式 2: 智能记忆（自动压缩）
memory = SmartMemoryWithCompaction(llm, max_active_memories=100)
await memory.remember("key", "长内容...")
content = await memory.recall("key")  # 可能是摘要
```

**工作原理**：
1. 跟踪每个节点的访问时间和次数
2. 超过阈值未访问 → 标记为"冷"
3. 冷节点调用 LLM 生成摘要
4. 释放原始内容，保留摘要
5. 访问时如果已压缩，返回摘要

### 人机协作（steer_work + side_questions）

运行时动态调整 Agent 行为，Agent 主动向人类提问。

```python
from runtime.intervention import HumanInterventionManager, SteerableAgent

manager = HumanInterventionManager()

# 注册可干预的 Agent
agent = SteerableAgent(
    agent_id="coder",
    instructions="你是程序员",
    llm=llm,
    intervention_manager=manager,
)

# 运行时干预
await manager.steer("coder", "改变方向，先处理安全漏洞")
await manager.pause_agent("coder", "等待用户确认")
await manager.resume_agent("coder")

### 流式输出（SSE / WebSocket）

实时推送执行过程，支持逐 token 输出。

```python
from runtime.streaming import StreamingExecutionLoop, SSEStream

loop = StreamingExecutionLoop(llm)

# 收集 SSE 流
async for chunk in loop.get_sse_stream():
    print(chunk)  # data: {"type": "token", "data": "Hello"}

# 执行任务（自动推送事件）
result = await loop.run_react_streaming("分析这段代码")
```

**事件类型**：
| 类型 | 说明 |
|------|------|
| `start` | 执行开始 |
| `token` | 新 token |
| `thinking` | 思考过程 |
| `tool_call` | 工具调用 |
| `tool_result` | 工具结果 |
| `end` | 执行结束 |

### 持久化状态（快照/恢复）

保存和恢复系统状态，防止意外丢失。

```python
from harness.persistence import SnapshotStore, CheckpointManager

# 创建存储
store = SnapshotStore("snapshots/")

# 保存快照
snapshot_id = await store.save(
    blackboard=bb,
    frontier=frontier,
    metadata={"task": "重要任务"}
)

# 加载快照
snapshot = await store.load(snapshot_id)

# 恢复黑板状态
await store.restore_blackboard(snapshot_id, bb)

# 自动检查点（每 5 分钟保存）
manager = CheckpointManager(store, interval_seconds=300)
await manager.start(lambda: {"blackboard": bb})
```

### 插件市场

发现和加载第三方插件（工具、Agent、技能）。

```python
from plugins.marketplace import PluginMarketplace

market = PluginMarketplace("plugins/")

# 发现插件
plugins = market.discover()

# 加载插件
plugin = await market.load("example_tools", context={"tool_registry": registry})

# 获取插件工具
if hasattr(plugin, 'get_tools'):
    tools = plugin.get_tools()
```

**插件类型**：
| 类型 | 说明 |
|------|------|
| `ToolPlugin` | 注册自定义工具 |
| `AgentPlugin` | 注册自定义 Agent |
| `SkillPlugin` | 注册自定义技能 |

### 分布式执行

多节点协作，任务队列和负载均衡。

```python
from runtime.distributed import ClusterManager

# 创建集群
cluster = ClusterManager()

# 创建执行器（不同能力）
coder = cluster.create_executor("node1", ["coding"])
tester = cluster.create_executor("node2", ["testing"])

# 提交任务
task_id = await cluster.submit_task("coding", {"code": "..."})

# 启动执行器
async def handler(task):
    return f"Completed: {task.payload}"

await coder.start(handler)
await tester.start(handler)

# 获取结果
result = await cluster.get_task_result(task_id)
```

**特性**：
- **任务队列**：优先级排序、能力匹配
- **节点发现**：注册表、心跳检测
- **负载均衡**：自动分配到空闲节点
- **故障转移**：节点离线自动重新分配

## 路线图

### 已完成 ✅

| 功能 | 说明 |
|------|------|
| ReAct 循环 | 灵活的单步推理执行 |
| Plan-and-Execute | 结构化任务执行 |
| 三种协作模式 | Router / Parallel / Debate |
| 分层黑板 | Global / Domain / Local 三层隔离 |
| 关键信息保护 | 永不过期、跨领域广播 |
| 事件驱动架构 | 黑板变更自动触发规划 |
| 意图队列 | 优先级排序、原子认领 |
| LLM 韧性 | 重试、熔断、限流、缓存 |
| Skills 技能 | PromptSkill / ToolSkill |
| 图状态黑板 | 节点+边，支持路径分析 |
| 冷节点压缩 | 长期未访问记忆自动摘要 |
| 人机协作 | steer_work、side_questions |
| 流式输出 | SSE / WebSocket |
| 持久化状态 | 快照保存和恢复 |
| 插件市场 | 第三方工具/Agent/技能 |
| 分布式执行 | 多节点任务队列 |

### 待实现 ⏳

| 功能 | 说明 |
|------|------|
| 图形化界面 | Web UI 监控和管理 |
| 更多 LLM 提供商 | Claude、Gemini、本地模型 |
| A2A 协议 | Agent2Agent 标准通信 |
