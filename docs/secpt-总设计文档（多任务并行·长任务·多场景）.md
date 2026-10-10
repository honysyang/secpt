# secpt 总设计文档 · 多任务并行 / 长任务 / 多场景

> 版本 v2.0 · 2026-10-07 · 面向"同时跑多个任务、单个任务可能跑 7×24 小时、任务类型横跨 CTF / 渗透测试 / 综合测试 / 安全评估 / 代码审计"
> 配套文件：`secpt-开发提示词S0-S7（可直接复制）.md`（纯提示词，不含分析）

---

## 0. 先回答一个问题：这次要加的复杂度，跟上次删掉的复杂度有什么不同

上一轮的结论是"复杂度是负债，除非被证明有收益"。那个结论没有变，变的是**需求场景**：

| 场景 | 上轮（单题 CTF） | 本轮 |
|---|---|---|
| 任务数 | 1 | 同时 N 个，互不干扰 |
| 时长 | 3 分钟 | 可能 7×24 小时 |
| 类型 | CTF 一种 | 5 类，产物和终止条件都不同 |
| 失败代价 | 重跑一次 | **可能已经跑了 18 小时** |

这三个新约束是**正交**的，每个都必须由一个新的层来承担，没有任何一层能顺带解决另一个：

- **并行** → 需要**作业层**（隔离、队列、配额）——核心不是吞吐，是**一个任务炸了不能带走别人**
- **长期** → 需要**时间与账务层**（checkpoint / resume / 上下文压缩 / 预算）——核心不是"跑得久"，是**崩了能接上**
- **异构** → 需要**任务型层**（profile 路由）——核心不是多架构，是**异构性用数据表达，不用代码表达**

而上一轮删掉的东西（六拍、评分、树搜索、Node 派生）**依然不入系统的任何路径**，因为它们是"执行器内部的复杂度"，而这次加的是"执行器外面的复杂度"。

**一句话：内核不动，外面加三层。**

---

## 1. 目标架构

```
┌───────────────────────────────────────────────────────────────┐
│ L4  运维面        secpt submit / ls / status / tail / cancel   │
│                   + 事件流回放 + 增量报告查看                    │
├───────────────────────────────────────────────────────────────┤
│ L3  作业层        JobManager：队列 / 并发上限 / 进程隔离 /       │
│     （新增）      每 job 独立 workspace / worker 生命周期 / 看门狗│
├───────────────────────────────────────────────────────────────┤
│ L2  编排层        Orchestrator：profile 路由 → specialist 拓扑   │
│     （新增）      → 派发 → ATT&CK 覆盖检查                       │
│                   入口数 == 1 时编排层休眠（直连单链）            │
├───────────────────────────────────────────────────────────────┤
│ L1  任务运行时    RoundPool：token/费用/时间预算 · 上下文压缩 ·  │
│     （新增）      会话轮转 · checkpoint/resume · 事件流 · 战情库  │
├───────────────────────────────────────────────────────────────┤
│ L0  内核 ⛔       Engine：messages 整条 append · tool_calls 执行 │
│     神圣区域      · 结果 append 回同一条链 · 命中终止条件退出      │
│                   ≈ 现有 runtime/harness.py 的主循环，一字不改语义│
└───────────────────────────────────────────────────────────────┘
```

**L0 是唯一执行器。** L1~L4 全部在它外面：它们决定"起哪条链、给什么 prompt、跑到什么时候该停下来存档"，但**永远不介入这条链内部正在进行的推理**。

这是整套设计唯一不可违反的红线。写成代码约束就是：

> **Engine.run() 内部不得 import JobManager / Orchestrator / Evaluator / GraphStore。**
> 违反这一条，整个系统立刻退化回三轮 0 分的形态。

---

## 2. 三条红线（不可协商）

### 红线 1 · 思维流不可切分

单个回合内：`messages` 只 append，从不重写、从不按拍重建、从不切换 system prompt。
模型生成的 token（含推理正文）一条不漏地进入下一次输入。

**唯一允许的例外**：Session Roll Over —— 当一个回合跑满预算边界（见 §4.3）时，写一份结构化交接摘要，用它开启**新的一条 messages 链**。这不是"切段推理"，而是"换班"：交接本把已确立的事实写实写全，新回合的链读完交接本就能接着往下推。人类的 24 小时轮班制同理。

### 红线 2 · 复杂度必须有对照数据

每引入一个层，必须能一句话说清"关掉它会怎样"，并且这个效果是可测量的。
缺省保守值：新组件**默认关闭**，只有尾对照实验证明它提升了成功率/换会计步数/换会计墙钟，才进默认路径。

拿不出对照数据的组件，一律降级为 `--mode legacy` 或 `--feature xxx=on`。

### 红线 3 · 异构用数据表达，不用代码表达

CTF 和代码审计的差异 **不在执行器**，在：system prompt 先验、工具子集、产物 schema、终止条件。
所以 5 种任务类型 = 5 个 YAML profile 文件，**零个新执行器**。

任何人提出"这个场景需要特殊架构"，默认回答是：先改成 profile 里的一段 prompt 试试。

---

## 3. 现有资产去留清单

> 当前代码：`runtime/harness.py`（505 行，已验证）+ `src/secpt/`（~3500 行）+ `scripts/` + `tests/`

| 模块 | 判定 | 处置 |
|---|---|---|
| `runtime/harness.py` 主循环 | ✅ **保留** | 抽成 `runtime/engine.py` 的 `Engine.run()`，语义一字不改 |
| 4 个基础工具（bash/read/list/write） | ✅ **保留** | 移到 `runtime/tools/`，白名单参数化 |
| `StuckDetector` 卡死切换器 | ✅ **保留** | 阈值为 profile 可配（不同场景灵敏度不同） |
| `call_llm` httpx 直连 | ⚠️ **改造** | 补指数退避、429/5xx 重试、回传时剔除 `reasoning_content` |
| system prompt 里的 ROIS 地址 | ❌ **删除** | 本次 CTF 遗留硬编码，必须参数化 |
| `core/react/*` 六拍 | ❌ **删除** | 不入任何路径 |
| `core/evaluator/` LLM 评分 | ❌ **删除** | 这是三轮 0 分的直接原因之一 |
| `core/node/` 分形派生 | ❌ **删除** | 被 specialist DAG 取代 |
| `core/scheduler/wavefront.py` | ♻️ **改造** | 从"按评分派生节点"改为"按入口/职责并行派发"，位置移到 L2 或 L3 |
| `core/graph/` GraphStore | ♻️ **改造** | 从"证据评分图"改为**战情表**：assets / credentials / findings / attempts / hypotheses |
| `core/tools/registry.py` 白名单命令注册 | ♻️ **改造** | 改为 profile 驱动的工具子集选择 |
| `governance/` scope·ratelimit·risk·audit | ✅ **升级** | 单作业 → **全局**：跨 job 的目标限速、全局 scope registry、不可变审计 |
| `telemetry/` | ✅ 保留 | 补 job_id / round 维度 |
| `report/` | ♻️ **改造** | 终态报告 → **增量产出**（每验证一个 finding 即刻落盘） |
| `challenge/` | ♻️ **改名** | → `core/profile/`，含 router + 5 个 YAML |
| `cli/chat.py` | ♻️ **改造** | 保留自然语言入口，但其下改为 submit job |
| `scripts/{replay,recheck}.py` | ♻️ **改造** | 改为 accept `--job-id`，从事件流重放/校验 |

**净效果**：净删除约 1200 行（六拍 + 评分 + Node），保留并改造约 1500 行，新增约 2000 行（L1~L3）。

---

## 4. 三个新层分别要解决什么

### 4.1 作业层（应对并行）

**第一诉求是隔离，不是吞吐。**

- **进程级隔离**：每个 Job 一个 OS 进程（`multiprocessing` spawn 或 `subprocess` 拉 worker.py）。理由有二：① 7×24 长任务下单个 worker 崩溃/卡死必须能被 kill 而不影响他人；② 现有 `bash` 工具用同步 `subprocess.run`，放在一个进程的 asyncio loop 里跑会**阻塞整个 event loop** —— 现在的 harness 是单任务所以没暴露，一并行立刻死锁。
- **Job 内工具并行**用 `asyncio.to_thread`，**Job 间并行**用进程。两个层次不要混。
- **每 Job 独立 workspace**：`/var/lib/secpt/jobs/<job_id>/`，含 `workspace/`（模型可写）、`rounds/`、`events.jsonl`、`artifacts/`。当前写死 `/tmp/harness` 在多任务下会互相踩。
- **并发上限**：`limits.max_parallel_jobs`（默认 4），超出进队列，FIFO + 优先级。
- **全局限速跨 Job**：两个 Job 打同一个目标时，限速必须是全局计数器，不是每 Job 一个（现有 `governance/ratelimit.py` 需要抬到 manager 进程里的单例）。
- **状态机**：`queued → running → paused → succeeded / failed / budget_exceeded / cancelled`，每个状态迁转事件流。

### 4.2 任务型层（应对 5 类任务）

TaskSpec 是唯一入口：

```yaml
job_id: string
objective: string            # 自然语言，全文给模型，不摘要
task_type: ctf | pentest | redteam | assessment | codeaudit
scope:                       # 授权范围，硬约束
  allow_hosts: []
  deny_hosts: []
  max_rps: int
budget:
  max_wall_s: int            # 7*24 -> 604800
  max_steps: int
  max_rounds: int
  max_cost_cny: float
artifacts:                   # 附件/源码路径，自动挂进 workspace
  - path: /path/to/src.zip
```

5 个 profile 的差异一览：

| profile | 主产物 | 拓扑 | 终止条件 | 工具子集 | 先验侧重 |
|---|---|---|---|---|---|
| `ctf` | flag | 单 Exploit 链（recon 可短暂辅助） | flag 正则 | 4 基础 + nmap(限) | 有附件先读源码 / grep 凭据 / 默认口令 |
| `pentest` | 漏洞 + PoC | recon 面探索 → 每入口一条 Exploit 链并行 | 预算耗尽 或 面穷尽 | + nmap/httpx/whatweb | 入口枚举优先 / 每个服务至少一次认证尝试 |
| `redteam` | 攻击链 + 覆盖报告 | multi-specialist 并行 + ATT&CK 覆盖检查 | 覆盖度达标 或 预算 | 全量 | 从初始访问到影响达成的完整链路，不追求单点漏洞 |
| `assessment` | 风险清单 | checklist 驱动（每项一条短线程） | 清单跑完 | 保守子集 | 合规项优先 / 不入 destructive |
| `codeaudit` | 缺陷 + 文件:行 + PoC | 按模块/按危险函数切分并行 | 代码库扫完 | 去掉 web 类，+ semgrep | 污点分析 / sink 优先 / 几乎不联网 |

**路由方式**：先规则匹配（关键词表），命中不了再用一次小模型调用分类。理由：路由错了代价高（跑 20 小时才发现 profile 不对），规则确定性优先。

### 4.3 任务运行时层（应对 7×24）

这是改动最大的一块，四件事缺一不可。

**(a) 会话轮转 Session Roll Over**

一条 messages 链不可能跑 24 小时：上下文会爆、API 会断、进程会被 OOM。所以按边界切回合：

```
rollover 触发（任一满足）：
  steps_in_round >= 60  |  wall_in_round >= 25min  |  est_tokens >= 55% window  |  一个子目标完成
rollover 动作：
  1. 生成结构化交接摘要 handoff（小模型生成，见下方格式）
  2. messages = [system(objective+profile+prior+scope), {role:user, content: handoff}]
  3. round += 1，继续
```

handoff 摘要 **必须是结构化的**，不能是自然语言流水账（流水账会丢关键事实且引入幻觉）：

```markdown
## 任务目标
<objective 原文，不改写>
## 已确认事实（附证据位置）
- http://x:59123 = IdP；http://x:43139 = SP  [step 3, 首页标题]
- admin / RCTF2025  [docker-compose.yml:20]
## 已获取凭据
- admin:RCTF2025 → IdP 登录成功，userType=0
## 已排除（不要再试）
- 自签名 SAMLResponse → 401（SP 用 IdP 证书验签，validator.py:49）
- 邀请码注册 → type 字段服务端固定
## 当前位置
正在构造 XSW：伪造 admin assertion 置于文档首位
## 下一步候选
1. 先走正常 SSO 拿到真实签名断言，再改文档顺序
2. 检查 validator 是否只验 Response 不验 Assertion
```

**(b) 上下文压缩 Compaction**

轮转解决不了单回合内的膨胀（一个 dense nmap 输出就几 KB）。同回合内的急诊压缩：

```
触发：est_tokens >= 0.55 * window
保护：system + objective + 最新 12 条消息 + 所有 finding/credential 记录
压缩对象：最老的 **整组** (assistant with tool_calls → 其全部 tool messages)
   ⚠️ 必须整组删除：DeepSeek 要求每个 tool_call 都有对应 tool message，
      只删一半会直接 400。这是实现时最容易踩的坑。
产物：在原位插一条 {"role":"user","content":"【历史摘要 #k】..."}
```

注意一个反直觉的点：**append-only 天然命中 DeepSeek 的 prefix cache**（便宜很多），而压缩会破坏这个命中。所以压缩策略必须是"忍到必须忍时再压"，而不是"定时压"。压完之后新的 prefix 重新建立，后续继续 append 又能命中。

**(c) 预算与账务**

```
每步累计 est_tokens / cost_cny / wall_s
到达 budget 的 80% → 注入一次"剩余预算提醒"（让模型收尾，别卡在探索中途）
到达 100% → 优雅停止：先把已验证 findings 落盘，生成阶段性报告，状态 budget_exceeded
```

**成果递减监控（长任务特有）**：每回合结束统计新增 findings / 资产 / 凭据数。**连续 3 个回合零新增** → 触发策略切换（换 profile 侧重点或派人），而不是继续烧。7×24 最危险的不是失败，是**安静地空转一整天**。

**(d) Checkpoint / Resume**

```
每 round 结束 → 写 rounds/NNN.messages.json
每步          → append events.jsonl（断电最多丢一步）
worker 重启   → 读最后一个 rounds/NNN.messages.json 恢复 messages，从 round NNN 继续
```

验收标准：**跑到一半 `kill -9`，重新 submit --resume，必须能从断点继续并最终完成。** 这条不做，7×24 不成立。

### 4.4 记忆的两层

| | 战情库（单 Job 内） | 经验库（跨 Job） |
|---|---|---|
| 存什么 | assets / credentials / findings / attempts / hypotheses | 已验证的打法模式、踩过的坑 |
| 介质 | SQLite（现有 graphstore.sqlite 改造） | YAML + 关键词检索 |
| 写入 | 每次工具结果里抽到结构化事实即写 | Job 结束且 flag/finding 验证通过后写 |
| 读取 | 注入 handoff 摘要 | profile 加载时按关键词被动注入 system prompt |

**不要做向量检索。** 渗透任务的事实量级（几百到几千条）用不上，而且可解释性差。

---

## 5. 风险控制

### 5.1 立即处理（不是建议，是必须）

**你现在暴露的目录服务极度危险。**

```
http://192.168.56.103:8080/          ← 暴露的是 Kali 的 $HOME
```

里面可以看到 `.ssh/`、`.git-credentials`、`.gnupg/`、`.bash_history`、`.kimi/`、`.burpsuite/` …… 任何人能访问到这个端口（同网段、或做了端口转发的宿主机），就能直接拿走你的 SSH 私钥和 git 凭据。**请现在就 kill 掉它。**

后续如需我读代码，改用： 一次性 `tar` 到临时目录 + 起服务 + 读完立刻 kill + 删归档。或者直接从 Kali 侧拷到工作区。

### 5.2 多任务并行后的升级风险

| 风险 | 阶段 | 措施 |
|---|---|---|
| 一个 Job 的 bash 打到别人的目标/网段 | S2 | 全局 scope registry + 每 Job 校验 |
| `nc` 类工具反弹 shell | S2 | 从默认白名单剔除，`pentest` profile 显式申请才给 |
| 两 Job 同时打同一目标打挂服务 | S3 | 全局限速单表，跨 Job 计数 |
| Job 无限烧钱 | S3 | `max_cost_cny` 硬闸 + 80% 提醒 |
| worker 僵死占着名额 | S3 | watchdog：15min 无 LLM 调用 → kill 并标记 `stuck` |
| 沙箱逃逸 | S7 | 每 Job 一个容器（后续），S2 先做进程+目录+白名单三级 |
| 密钥泄露 | 全程 | 只走环境变量；`audit/` 里不得出现任何 sk- |

---

## 6. 开发规划总表

| 阶段 | 主题 | 交付物 | 通过判据 |
|---|---|---|---|
| **S0** | 内核外提 | `Engine` 类 + 参数化 + 剔除硬编码 | 重跑 ROIS **仍拿到 flag**，步数/墙钟与 baseline（69 步 / 204s）同量级 |
| **S1** | 持久化 + 事件流 + resume | events.jsonl / rounds / SQLite job 表 | `kill -9` 后 `--resume` 能续跑并拿 flag |
| **S2** | 作业层 + 并行 worker | JobManager / worker 进程 / CLI | 同时跑 3 个 Job，互不干扰，各自出结果 |
| **S3** | 任务运行时：压缩 + 预算 + 轮转 +  watchdog | compaction / budget / roundpool / watchdog | 人为把 context 窗口调到 8K 逼压缩 + 60 步轮转，**仍能拿 flag** |
| **S4** | Profile 路由 + 5 个 profile | router + 5 YAML | CTF 题走 ctf profile 跑通；自建含漏洞仓库走 codeaudit 跑出缺陷 |
| **S5** | Specialist MAS + 编排 | recon/exploit/verify/analysis + DAG | 多入口靶场（≥2 入口）产出带复现步骤的 finding |
| **S6** | 验证器 + 增量报告 | verify agent + 增量 report | finding 必须带可独立复现的 PoC，否则不进报告 |
| **S7** | 生产化治理 | Docker 沙箱 / 全局限速 / 成本dashboard | 同目标双 Job 并发不超限；单 Job 炸不影响其他 |

**排序原则**：S0~S3 是"能跑多久都不会丢"，S4~S5 是"能打几类仗"，S6~S7 是"结果可信且不出事"。
**任何阶段不得跳过验收。** 原因是上一轮每一版修改都"看起来在正常工作"，但三轮实战全是 0 分——所以这次每个阶段末尾都有硬性判据，判据不过不许进下一阶段。

### 时间口径

S0+S1 是地基，**必须最先完成**；S2 之后才谈得上并行；S3 之后才谈得上 7×24；S4 之后才谈得上多场景。
**不要试图一次性完成。** 每阶段开局前先跑一次上一阶段的验收脚本确认没退化。

---

## 7. 一句话总结

> **内核（一条不断裂的 messages 链）已经被证明能解题。这次要做的全部事情，是让这条链能够：被并发地起很多条、被安全地停在某处并从那里接上、被喂正确的 prompt 去打不同的仗。**
>
> 做这三件事需要加三层。但三层都在链**外面**。谁要是把逻辑写进 `Engine.run()`，系统就会在三处 API 调用内变回那台零件齐全但不解题的机器。

---

## 附：本次新增矢量的位置一览

```
secpt/
├── runtime/
│   ├── engine.py            # L0 ⛔ 神圣区域（从 harness.py 抽出）
│   ├── tools/{shell,fs,web,static}.py
│   └── llm/client.py        # 退避重试 + fallback + 剔除 reasoning_content
├── core/
│   ├── job/{models,store,manager,worker}.py       # L3
│   ├── context/{estimator,compaction,budget,roundpool}.py  # L1
│   ├── profile/{loader,router}.py  +  profiles/*.yaml       # L2
│   ├── orchestrator/{specialist,dag,policy}.py             # L2
│   ├── memory/{warroom,experience}.py
│   ├── watchdog.py
│   └── governance/{scope,ratelimit,risk,audit}.py  # 升级全局
├── cli/app.py               # L4 submit / ls / status / tail / cancel / resume / report
└── var/jobs/<job_id>/{workspace,rounds,events.jsonl,artifacts,report.md}
```

---

*配套：完整的可粘贴提示词见 `secpt-开发提示词S0-S7（可直接复制）.md`*
