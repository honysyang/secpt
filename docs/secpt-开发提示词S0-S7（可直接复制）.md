# secpt 开发提示词 S0–S7（可直接复制）

> 配套《secpt-总设计文档（多任务并行·长任务·多场景）.md》
> 用法：**一次复制一个阶段**粘给 Kali 上的开发 agent。阶段验收没过，不许开始下一阶段。
> 每条提示词里的「禁止事项」是硬约束，跟"要做什么"同等重要。

**通用前置（每个阶段开头都要确认一次）**：

```bash
cd /home/kali/secpt && git status && source .venv/bin/activate && python -c "import secpt, sys; print(sys.version)"
export DEEPSEEK_API_KEY=...   # 只走环境变量，任何文件里不得出现 sk- 明文
```

**基线数据（贯穿全程，不能退化）**：ROIS SAML SSO 题 → **69 步 / 203.7s / FLAG = RCTF{e946a54f-75cf-6d73-9163-ba0959c2dafc}**

---

## S0 · 内核外提：把 harness.py 从脚本变成可复用 Engine

```
目标：把 /home/kali/secpt/runtime/harness.py（505 行，已验证能独立解题）
抽取成一个参数化的 Engine 类，主循环语义一字不改，但不再含任何硬编码的任务信息。

这是后续所有阶段的地基。做完之后，Engine 必须能被流水线之外的任意代码直接调用起来。

=== 具体任务 ===

1. 新建 src/secpt/runtime/engine.py
   把 harness.py 的主循环搬进来，做成：

   @dataclass
   class RunConfig:
       job_id: str
       objective: str              # 任务全文，原文传入，不摘要
       system_extra: str           # profile 渲染出的先验+工具说明+约束
       workspace: Path
       max_steps: int = 100
       max_wall_s: int = 900
       bash_timeout_s: int = 60
       bash_whitelist: frozenset[str] = frozenset({...})
       flag_regex: str | None = None
       model_primary: str = "deepseek-chat"
       model_fallback: str = "deepseek-reasoner"
       stuck_threshold: int = 6
       stuck_increment: int = 2

   @dataclass
   class RunResult:
       flag: str | None
       steps: int
       wall_s: float
       status: str                 # success | budget_exceeded | failed | error
       messages: list[dict]        # 完整 transcript

   class Engine:
       def __init__(self, cfg: RunConfig, llm, tools, hooks=None): ...
       async def run(self) -> RunResult: ...

2. 主循环必须原样保留这四条语义，不许改：
   a) messages 整条 append，从不 rebuild、从不按拍重建、从不切换 system prompt
   b) assistant 消息原样 append（含 content 与 tool_calls），不做任何裁剪
   c) 每个 tool_call 的结果 append 回同一条 messages
   d) 命中 flag_regex 立即退出并落 transcript

3. 从 system prompt 里彻底删除这些硬编码（这是本次 CTF 的遗留物）：
   - "http://oj-10-30-15-1-59123..." 和 "...43139..." 两个具体地址
   - "只许碰题面里给出的目标地址"这句固定表述
   改为：由 RunConfig.system_extra 传入，scope 约束改由 governance 层负责。
   system prompt 的骨架固定为：
       你是{角色}。\n\n【任务全文】\n{objective}\n\n{prior}\n\n【可用工具】\n{tools_doc}\n\n【约束】\n{constraints}

4. PRIOR_KNOWLEDGE（harness.py 里的 7 条打法先验）移到
   src/secpt/knowledge/priors/ctf.md，由 profile 加载，不再写在 .py 里。

5. 常量全部去掉全局写死：MAX_STEPS / BASH_TIMEOUT / BASH_MAX_OUT /
   READ_MAX / WORK_DIR / BASH_WHITELIST 全部改为 RunConfig 字段，
   WORK_DIR 改为 <workspace>/ws，每个 job 一个（当前写死 /tmp/harness，
   多任务下会互相踩，必须改）。

6. 新建 src/secpt/runtime/llm/client.py，把 call_llm 搬进去并补三点：
   a) 指数退避重试：429 / 5xx / ReadTimeout 最多重试 4 次，间隔 1/2/4/8 秒 + 抖动
   b) 回传 messages 时**剔除 reasoning_content** —— 它只能落盘用于审计，
      放进下一轮请求会被 DeepSeek 拒或污染上下文。这是当前实现的潜在坑。
   c) 单次请求 timeout 提到 180s（60s 在长推理时会误杀）

7. StuckDetector 原样搬到 src/secpt/runtime/stuck.py，阈值改由 RunConfig 传入。
   消息注入角色从 "system" 改为 "user"（DeepSeek 对中途 system 消息的处理不稳定）。

8. 保留一个兼容入口：python -m secpt.runtime.legacy --objective-file X --attach-dir Y
   行为与今天的 harness.py 完全一致，用作回归对照。

=== 禁止事项（违反任一条视为本阶段未完成）===
- 不许在 Engine.run() 里 import 任何 job / orchestrator / evaluator / graph 模块
- 不许在主循环里加"评估"、"打分"、"分支选择"逻辑
- 不许引入 LangGraph / openai SDK / pydantic-ai 等外部编排依赖
- 不许改工具数量：仍然只有 bash / read_file / list_dir / write_file 四个
- 不许给 write_file 开放 workspace 之外的路径
- 单测只写一个即可：test_engine_smoke.py，
  用一个假 LLM（返回固定 tool_call 再返回 ANSWER）验证主循环四步能跑通

=== 验收 ===
cd /home/kali/secpt
secpt run --profile ctf --objective-file ~/challenges/rois-saml-sso/test.md

判据（三条全满足）：
1. 拿到 FLAG：RCTF{e946a54f-75cf-6d73-9163-ba0959c2dafc}
2. 步数在 69 步 ±25 步内（即 44~94），墙钟 < 400s
   超出说明 Engine 提取过程中改变了行为，回去 diff harness.py
3. grep -r "oj-10-30-15-1" src/ 结果为空（硬编码已清除）

把终局打印（步数/墙钟/FLAG）和 pytest -q 结果贴回来。
```

**预计改动**：新增 ~350 行，删除 ~505 行（harness.py 退役），净减 150 行。

---

## S1 · 持久化：事件流 + checkpoint + resume

```
目标：让任务跑到一半被杀掉，重启后能从断点继续。不做这一条，7×24 就不成立。

前置：S0 验收已过。

=== 具体任务 ===

1. 目录约定
   var/jobs/<job_id>/
     ├── task.yaml          # 原始 TaskSpec 快照
     ├── events.jsonl       # append-only 事件流（断电最多丢一步）
     ├── rounds/000.messages.json, 001.messages.json, ...
     ├── ws/                # 模型可写的工作区（原 /tmp/harness 的角色）
     ├── artifacts/         # 附件/下载的源码，只读挂载
     └── report.md          # 增量报告（S6 才填满，这里先建空壳）

2. src/secpt/core/job/models.py
   TaskSpec(fields): job_id, task_type, objective, priority,
                     allow_hosts, deny_hosts, max_rps,
                     budget{wall_s, steps, rounds, cost_cny}, artifacts[]
   JobRecord(fields): job_id, spec, status, round, steps, cost_cny,
                      created_at, updated_at, last_error
   status ∈ queued|running|paused|succeeded|failed|budget_exceeded|cancelled|stuck

3. src/secpt/core/job/store.py  —— SQLite，改造现有 data/graphstore.sqlite
   连接必须设置：
     PRAGMA journal_mode=WAL;  PRAGMA busy_timeout=5000;
   （后续多进程并发写，没有这两条必死锁）
   tables:
     jobs(job_id PK, spec_json, status, round, steps, cost_cny,
          created_at, updated_at, last_error)
     events(seq INTEGER PK AUTOINCREMENT, job_id, ts, round, type, payload_json)
     findings(job_id, id, kind, value, evidence, verified, ts)

4. src/secpt/core/job/event.py
   EventSink 类，写 events.jsonl + 转存 SQLite（双写，SQLite 失败不阻断）。
   事件类型固定为这几种，不许扩展：
     assistant / tool_call / tool_result / finding / round_end
     / budget_warning / rollover / status_change / error
   tool_result 的 payload 必须含 status ∈ ok|empty|error 和 bytes，
   便于后续判断"这一步到底有没有产出"。

5. Engine 侧挂钩：把 S0 留下的 hooks 参数用起来
   hooks.on_step(idx, elapsed_s)  → 每步一次
   hooks.on_assistant(msg_dict)   → 写 assistant 事件
   hooks.on_tool(name, args)      → 写 tool_call 事件
   hooks.on_result(call_id, out)  → 写 tool_result 事件
   hooks.on_round_end(round, messages) → 落 rounds/NNN.messages.json

6. resume 实现（核心）
   round pool:
   def save_round(job_id, n, messages, handoff) -> Path
   def load_latest_round(job_id) -> tuple[int, list[dict]]
   resume 流程：
     n, messages = load_latest_round(job_id)
     如果 messages 为空或 n 不存在 → 从第 0 轮开始
     否则以该 messages 作为 Engine 的初始链继续
   注意：resume 后 messages 里不能有"孤立 tool_call"（有 tool_call 但缺
   对应 tool message 会 400）。落盘前做一次 sanitize：剔除最后一组不完整的
   assistant+tool_call。

=== 禁止事项 ===
- 不许用 pickle 存 messages，只许 JSON
- 不许把 API key 写进 events/task.yaml/SQLite
- 不许在落盘时对 messages 做"摘要"（本阶段只存档不压缩，压缩是 S3 的事）
- 不许引入 Redis / 消息队列

=== 验收 ===
# 1. 先跑，跑到 30 步左右手动 kill -9
secpt run --task /tmp/task-rois.yaml &
sleep 45 && kill -9 %1
secpt resume <job_id>
判据：
  1. resume 之后 ward 必须从 events 里能读到的最后一步之后继续
     （检查 events.jsonl 的 seq 连续性，不能重头再来）
  2. 最终拿到同一个 FLAG
  3. rounds/ 目录下至少有 1 个 NNN.messages.json
  4. jobs 表里该记录的 status 最终为 succeeded

# 2. 单测
pytest -q tests/unit/core/job/
  test_store.py       WAL 下并发写不死锁
  test_resume.py      落盘→加载→messages 完全一致（含 sanitize）
  test_events.py      事件 seq 连续，tool_result 带 status

把 resume 前后的 events.jsonl 头尾各 5 行 + flag 打印贴回来。
```

**预计改动**：新增 ~450 行。

---

## S2 · 作业层：多任务并行

```
目标：同时跑多个任务，互不干扰。注意第一诉求是**隔离**，不是吞吐。

前置：S1 验收已过。

=== 具体任务 ===

1. 为什么必须是进程隔离（想清楚再动手）
   现有 bash 工具用同步 subprocess.run()。如果多 job 跑在同一个进程的
   asyncio loop 里，一个 nmap 60s 会把所有其他 job 全部阻塞。
   所以：
     Job 间并行 = OS 进程
     Job 内多条 tool_call 并行 = asyncio.to_thread
   这两个层次不许混用。

2. src/secpt/core/job/worker.py
   独立进程入口：python -m secpt.core.job.worker --job-id <id>
   职责：加载 TaskSpec → 建 workspace → 起 Engine → 写事件 → 退出码反映结果
   退出码：0 succeeded / 1 failed / 2 budget_exceeded / 3 error
   信号：SIGTERM → 保存当前 round 后优雅退出；SIGKILL 由 S1 的 resume 兜底

3. src/secpt/core/job/manager.py
   JobManager: submit(spec) / cancel(job_id) / list() / reap()
   - max_parallel_jobs（config.yaml，默认 4），超出进队列 FIFO
   - 用 subprocess.Popen 起 worker，维护 pid 表
   - reap() 轮询子进程状态，更新 SQLite
   - 心跳：worker 每 10s 更新 jobs.updated_at；manager 发现某 worker
     15min 未更新且进程还活着 → 标记 stuck 并 SIGTERM

4. 每个 job 独立 workspace
   var/jobs/<job_id>/ws/    → 传给 RunConfig.workspace
   write_file 只允许落在这个目录内
   bash 的 cwd 设为该目录

5. 全局限速（重要，容易被漏）
   governance/ratelimit.py 目前是单对象，多 job 下会各算各的。
   改为 manager 进程内的单例，按 target_host 计数，跨 job 生效。
   所有 bash 发起的网络请求走一次 hook：解析命令里的 host → 检查配额。

6. CLI
   secpt submit  --task FILE        提交任务
   secpt ls                         列出 job（状态/步数/耗时/花费）
   secpt status <job_id>            详情
   secpt tail  <job_id> [-n 50]     从 events.jsonl 回放最近 N 条
   secpt cancel <job_id>
   secpt resume <job_id>

=== 禁止事项 ===
- 不许把 worker 写成线程或 asyncio task
- 不许让 worker 直接 import manager（worker 只认 job_id）
- 不许允许多 job 共用同一个 ws 目录
- 白名单里移除 nc（反弹 shell 风险），pentest profile 需要时显式申请

=== 验收 ===
# 三道题同时提交（用同一个 ROIS task 复制三份即可，job_id 不同）
for i in 1 2 3; do secpt submit --task /tmp/rois-$i.yaml; done
secpt ls            # 应看到并发 <= max_parallel_jobs
while secpt ls | grep running; do secpt ls; sleep 20; done

判据：
  1. 三个 job 全部 succeeded，各自 FLAG 一致
  2. 三个 job 的 ws/ 目录互相独立，没有任何跨写
     （跑完 ls -R var/jobs/<id1>/ws 与 <id2>/ws，不得出现对方文件）
  3. 单 job 耗时 <= 单跑耗时 × 1.3
     超了说明还在互相阻塞，回去查 subprocess 是否在 loop 里同步跑
  4. 中途 kill 掉其中一个 worker pid，其余两个必须照常完成
  5. pytest -q tests/unit/core/job/test_manager.py
     （并发上限生效、队列 FIFO、cancel 生效、stuck 检测）

贴回：secpt ls 的完整输出 + 三个 job 的耗时对比。
```

**预计改动**：新增 ~600 行。

---

## S3 · 任务运行时：7×24 的四件套

```
目标：让一条链能安全地跑几十个小时。这是改动最大的一个阶段，分四块。

前置：S2 验收已过。四块可以分四次提交，但验收要一起跑。

=== A. token 估算与预算 ===

src/secpt/core/context/estimator.py
  中文按 1 字 ≈ 0.7 token，ASCII 按 1 token ≈ 4 字符；
  保守起见统一取 max(len)/2，再乘 1.1 安全系数。
  不用 tiktoken（DeepSeek 分词器不公开，近似没意义）。

src/secpt/core/context/budget.py
  BudgetTracker: 累计 prompt_tokens / completion_tokens / cost_cny / wall_s
  单价从 config.yaml 读（per 1K input / output），不写死数值。
  阈值行为：
    达到 80% → 往 messages 追加一条 user 消息：
      "预算已用 80%。请停止探索，把你已确认的事实整理成结构化结论输出。"
    达到 100% → 优雅停止：先落 findings + 阶段性 report.md，
                status = budget_exceeded（不许直接抛异常丢失已有成果）

=== B. 上下文压缩 Compaction ===

src/secpt/core/context/compaction.py
  触发：est_tokens >= 0.55 * window
  保护：system 消息 + objective + 最新 12 条消息 + 所有已记录 finding/credential
  压缩对象：最老的一批，**必须按整组删除**
    (assistant[含 tool_calls] + 它的全部 tool messages) 是一组
    ⚠️ 只删 assistant 不删 tool result，或反过来，都会导致 API 400。
       这是本阶段最容易踩的坑，写完之后必须用一个多 tool_call 的假数据验证。
  产物：在被删区间原位插入一条
    {"role":"user","content":"【历史摘要 #k】\n<结构化摘要>"}
  摘要生成：调用 deepseek-chat（便宜模型）生成，prompt 强制输出这六段：
    1. 任务目标（原样复述 objective）
    2. 已确认事实（每条附证据来源，如 file:line 或 step N）
    3. 已获取凭据
    4. 已排除的尝试（务必写清，防止模型重复踩坑）
    5. 当前进展到哪一步
    6. 下一步候选（2-3 条）
  反直觉提醒：append-only 天然命中 DeepSeek 的 prefix cache（更便宜），
  压缩会打破这个命中。所以阈值不能太激进，0.55 是实测过的平衡点，别调低。

=== C. 会话轮转 Session Roll Over ===

src/secpt/core/context/roundpool.py
  触发（任一满足）：
    steps_in_round >= 60 | wall_in_round >= 25min
    | est_tokens >= 0.55 window | 一个子目标完成
  动作：
    1. 用 B 的同一套结构化摘要模板生成 handoff
    2. messages = [system(objective+profile+scope+prior),
                   {role:user, content:"【上一回合交接】\n" + handoff}]
    3. round += 1，累计步数不重置（总预算看累计）
    4. 落 rounds/NNN.messages.json
  ⚠️ rollover 是唯一允许"重建 messages"的地方。除此之外 Engine 内部
     任何位置都不许重建。给这个模块加一行注释说明白。

=== D. 成果递减监控 + watchdog ===

src/secpt/core/watchdog.py
  每回合结束统计：新增 assets / credentials / findings 数量
  连续 3 个回合零新增 → status=paused + 告警，并把"换层"提示注入下一回合 system_extra
  （7×24 最危险的不是失败，是安静地空转一整天）

Engine 侧改动：把 hooks 补齐 on_tokens(prompt, completion) / on_finding(kind, value, evidence)

=== 禁止事项 ===
- 不许让摘要参与"决策"：摘要只用于重建上下文，不许据此评分/剪枝/改目标
- 不许在单回合内除了 Compaction 之外的任何位置删改 messages
- 不许用摘要去替代工具原始结果做判断
- 预算到 100% 不许抛异常，必须走优雅收尾

=== 验收 ===
# 关键测试：把环境变量 SECPT_FAKE_WINDOW=8000 打开（把窗口假装调到 8K）
#   强制触发压缩 + 轮转多次
SECPT_FAKE_WINDOW=8000 secpt run --task /tmp/rois.yaml

判据：
  1. **仍然拿到同一个 FLAG** —— 这是硬指标，拿不到说明压缩或轮转丢了关键信息
  2. rounds/ 目录下出现 >= 2 个 messages.json（确实发生了轮转）
  3. events 里出现 compaction 与 rollover 事件，且摘要含"已排除"段落
  4. 最终 cost 相比不分轮的那次增长不超过 40%
  5. 单测：
     test_compaction.py  压缩后 messages 里没有孤立 tool_call（模拟请求不报错）
     test_budget.py      到 100% 优雅停，findings 已落盘，status=budget_exceeded
     test_rollover.py    handoff 摘要能作为新链起点继续推进

贴回：round 数量、总步数、总耗时、cost、以及其中一份 handoff 摘要原文。
```

**预计改动**：新增 ~700 行。

---

## S4 · Profile 路由与五种任务类型

```
目标：同一套 Engine，靠不同的 profile 打五类不同的仗。
核心原则：**异构用数据表达，不用代码表达**。五个 profile = 五个 YAML，零个新执行器。

=== 具体任务 ===

1. src/secpt/core/profile/profiles/*.yaml，五个文件：

ctf.yaml        role: CTF 解题手
                tools: [bash, read_file, list_dir, write_file, nmap(受限)]
                prior: priors/ctf.md（现有 7 条）
                terminator: flag_regex "RCTF\\{[^{}]{3,}\\}"
                budget: steps 100, wall 900s
                note: 有附件必须先下载解压读源码

pentest.yaml    role: 渗透测试员
                tools: + httpx, whatweb, nikto(可选)
                prior: priors/pentest.md（新增）
                  要点：先枚举入口；每个服务至少做一次认证尝试；
                       遇到 CMS/已知组件先查公开 CVE 再看手写逻辑
                terminator: 验证过的可利用漏洞 + PoC，或预算耗尽
                budget: steps 300, wall 3600s, rounds 20

redteam.yaml    role: 红队操作员
                prior: priors/redteam.md
                  要点：关注从初始访问到影响的完整链路，不追求单点漏洞；
                       每完成一步记录 ATT&CK 技术编号
                terminator: 覆盖度达标或预算耗尽
                budget: wall 28800s（8h）

assessment.yaml role: 安全评估员
                prior: priors/assessment.md
                  要点：以检查清单驱动，逐项验证；禁止破坏性动作；
                       发现即记录风险等级（高/中/低）+ 依据
                terminator: 清单跑完
                budget: wall 28800s

codeaudit.yaml  role: 代码审计员
                tools: [bash, read_file, list_dir, write_file, semgrep]
                prior: priors/codeaudit.md
                  要点：先列目录树摸清架构；按危险函数/sink 反查调用链；
                       静态优先，几乎不联网；每个缺陷给 文件:行 与最小 PoC
                terminator: 目标路径遍历完毕
                budget: steps 400, wall 7200s

2. src/secpt/core/profile/loader.py
   加载 YAML → Profile dataclass(role, tools, prior_path, terminator, budget, constraints)
   tools 字段决定给 LLM 的 function calling 列表（工具子集由此生效）

3. src/secpt/core/profile/router.py
   两级路由：
     第 1 级：规则关键词匹配（flag/CTF → ctf；代码仓/审计/源码 → codeaudit；
              合规/评估/基线 → assessment；红队/攻击链 → redteam；否则 → pentest）
     第 2 级：规则没命中 → 用 deepseek-chat 做一次 5 分类，temperature=0
   理由：路由错了代价极高（可能白跑 20 小时），规则确定性优先于 LLM 灵活性。
   路由结果必须落 events（type=routed），便于事后核查。

=== 禁止事项 ===
- 不许为任何一种任务类型新建执行器或子类 Engine
- 不许用 pip 装重型依赖；semgrep 缺失时降级为 grep 模式，不许报错退出
- prior 文件一律放 knowledge/priors/，不许内联在 .py 里

=== 验收 ===
# A. CTF（回归）
secpt run --profile ctf --objective-file ~/challenges/rois-saml-sso/test.md
  → 拿到同一个 FLAG（证明 profile 化没有破坏原能力）

# B. 代码审计（新能力，需要自建靶子）
mkdir -p /tmp/audit-target && 写一个含 3 个明显漏洞的 Python Flask 小应用
  （SQL 注入拼接、pickle.loads 反序列化、路径穿越读文件）
secpt run --profile codeaudit --objective "审计 /tmp/audit-target 下的 Python 代码，
  找出安全缺陷，给出 文件:行 和最小 PoC"
  → 至少命中 2/3 个缺陷，每条带 file:line

# C. 路由单测
pytest -q tests/unit/core/profile/test_router.py
  五类典型输入各给 3 条，规则层命中率 >= 80%

贴回：三个 profile 各自的输出片段 + router 命中率。
```

**预计改动**：新增 ~400 行 + 5 个 YAML + 4 个 prior md。

---

## S5 · Specialist 编排（MAS）

```
目标：多入口任务下并行推进。注意：**入口数 == 1 时编排层休眠**，直连单链。

依据：2026 HPTSA 基准显示多智能体相较单智能体有约 4.3× 提升，
但前提是"每个 agent 内部是一条完整不可断裂的链"，
而不是把一次本该连贯的调用拆成多个互不相干的短采样。

=== 具体任务 ===

1. src/secpt/core/orchestrator/specialist.py
   四个角色，每个都是一次 Engine.run()，只是 profile 片段不同：
     recon      → 枚举资产/端口/服务/目录，产出 structured asset list
     exploit    → 针对一个具体入口一条链（并行单位）
     analyze    → 白盒场景：读源码 / 数据流追踪（对应 codeaudit）
     verify     → S6 实现，本阶段先留空

2. src/secpt/core/orchestrator/dag.py
   最小 DAG：recon → [exploit × N 并行] → verify
   N = 入口数量。若 N == 1 → 跳过 DAG，直接跑一条 exploit 链。

3. src/secpt/core/memory/warroom.py —— L2 共享作战记忆（新系统的主干）
   SQLite 表：
     assets(job_id, host, port, service, tech, source_step)
     credentials(job_id, kind, value, source_step, valid)
     findings(job_id, id, kind, title, evidence, severity, verified)
     attempts(job_id, key_hash, action, result, reason)   ← 去重靠它
   角色之间**只交换这四张表的行**，不交换自然语言摘要。
   写 attempts 时用 key = sha1(action + target)，重复命中直接跳过。

4. src/secpt/core/orchestrator/policy.py
   明确不做的事：不做 LLM 评分、不做树搜索、不做 DeathCertificate 降级。
   （现有 evaluator/ 与 core/node/ 在此阶段正式删除）

=== 禁止事项 ===
- Orchestrator 不许读取 Engine 内部的 messages
- 不许把 Node 评分、证据强度打分这类概念重新引进来
- 单入口场景下不许启动 DAG（会白白多花一次 recon 的钱）

=== 验收 ===
# 多入口靶场：起两个已知漏洞容器（或用两个不同端口的服务凑双入口）
secpt run --task /tmp/multi-entry.yaml
判据：
  1. events 里出现 >= 2 条并行 exploit 链（job 内部 round/step 交织）
  2. warroom SQLite 的 assets / attempts 表有数据
  3. 至少 1 条 verified=1 的 finding
  4. 单入口的 ROIS 题仍走单链（events 里没有 recon→exploit 的 DAG 痕迹）
  5. 用一个 --no-orchestrator 开关做对照：多入口场景下 DAG 版
     必须比单链版发现更多 finding，否则 orchestrator 不许进默认路径（红线 2）
```

**预计改动**：新增 ~500 行，删除 ~1200 行（evaluator + node）。

---

## S6 · 验证器与增量报告

```
目标：假阳性是渗透自动化最致命的问题。报告里每一条都必须能独立复现。

=== 具体任务 ===

1. src/secpt/core/orchestrator/specialist.py 补 verify 角色
   输入：一条 finding（标题 + 证据 + 原始利用步骤）
   行为：开一个**全新的 Engine 实例、全新的 workspace、全新的 HTTP session**，
        严格按照 finding 描述的步骤**独立重做一遍**
   判定：只有在干净环境里重做成功，才 verified=1
   （为什么要全新 session：防止利用其实依赖了前一条链的 cookie/临时文件）

2. 增量报告
   每产生一条 verified finding，立即 append 到 var/jobs/<id>/report.md
   —— 不要在任务结束才写。7×24 崩了也要留下已确认的成果。
   报告格式：标题 / 严重等级 / 受影响目标 / 复现步骤（可直接粘贴执行）/
            证据截图或响应片段 / PoC 脚本路径

3. secpt report <job_id> 导出 Markdown 或 HTML

=== 验收 ===
1. 找一个真实 finding（ROIS 的默认凭据即可）
   → verify 角色在干净 workspace 独立重做，必须能再拿一次 FLAG
2. 故意造一条假 finding（比如写一个不存在的文件上传点）
   → verify 必须判定失败，且该条不进 report.md
3. 任务被 kill 在半途时，report.md 里已有之前 verified 的条目
```

---

## S7 · 生产化治理

```
目标：多任务长时间自动运行时，不把环境搞坏、不超范围、不烧爆预算。

1. 每 Job 一个容器
   secpt-sandbox 镜像：python + 常用工具 + 无外网默认策略
   worker 启动时 docker run --rm -v <job ws>:/ws --network secpt-net
   拿不到 docker 时的降级：进程 + 独立 HOME + 独立 cgroup 内存上限

2. 全局 scope registry
   所有 job 的 allow_hosts 汇总；任何 bash 命令解析出的目标不在白名单 → 拒绝并返回错误给模型
   （返回错误而不是静默丢弃，让模型知道自己被拦了）

3. 成本控制
   config.yaml 配 global_daily_cost_cap（元）
   manager 累计当日花费，超限拒绝新 job + 告警

4. 审计不可变
   data/audit/ 下按天分文件，append-only；不得出现任何密钥明文

5. 简易 dashboard（可选）
   secpt dash  起一个本地 HTTP，展示：运行中 job / 每 job 步数-耗时-花费 / 最近事件流

=== 验收 ===
1. 两个 job 同时打同一个目标 → 全局限速生效，总 RPS <= max_rps
2. 提交一个 allow_hosts 之外的 target → 被拒，stderr 打印明确原因
3. 单 job 容器内 rm -rf / → 宿主机不受影响
4. 当日花费达 cap → 新 submit 被拒并有提示
```

---

## 附：每阶段结束必须回答的三个问题

```
1. 这一步我加了什么复杂度？关掉它，指标会变差多少？（红线 2）
   答不上来 → 该组件不该在默认路径里。
2. 这一步有没有把逻辑写进 Engine.run()？（红线 1）
   有 → 撤回。
3. 这一步的差异能不能用 YAML 表达而不是代码表达？（红线 3）
   能但要写代码的 → 重写。
```
