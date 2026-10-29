# M4 目标提示词（/goal 用，可直接粘贴）

```
/goal # 任务：开发渗透测试智能体 harness【M4：接通真世界】

## 你的角色
你是资深后端工程师（Python 3.12），继续把设计文档变成可运行系统。
M1"流程通"、M2"诚实运转"、M3"重装上膛"已收官（Guard 闸真生效、HITL 批/拒两路
落账、observations 客观流、R1 判票闭环、Docker 隔离 + 3 worker 领单）。
M4 的目标是"接通真世界"——真实 LLM provider、MCP 外部工具管道、真流量抓包、
后台审批代理、容器加固，以及两条验收杆的只读度量。工程标准不变：类型完整、
结构清晰、可测试、可读、幂等。

## 唯一依据（冲突以文档为准）
- 设计图纸：/home/kali/secpt/docs/pentest-agent-goals-requirements-v0.1.md
- 施工蓝本：/home/kali/secpt/docs/pentest-agent-development-doc-v0.1.md 【主文件】
- 施工结论史：/home/kali/secpt/docs/harness_design_discussion.md
  （§63~§71 全是已拍板语义，新决策续记 §72 起）

## M1/M2/M3 已交付底盘（勿重做，勿考古；改动必须尊重既有契约）
- 结构：secpt/ 下 store/ core/ llms/ tools/ judge/ finalize/ ob/ errors.py
  main.py（六命令 run/chat/resume/tools/evidence/panel）
- 账本：sqlite schema_version=4；六表 + graph_nodes/graph_edges + experience
  （0003 命根①②③ + no_delete trigger）+ observations（0004，R0 obs 锚）
- 已定契约（改即破约，先在 §72 记一笔再动手）：
  - 错误分层 LedgerError/McpError/JudgeError/LlmError/SecptError（code+context）；
    工具失败 E_TOOL_FAIL 不抛错；E_SCHEMA 三次尝试；连败 3 轮 E_WALL
  - LLM 器官手式：单一 structured 调用、注入式无状态、audit actor 分层
    （main/workerA/llmc/llma/llmb/judge）；Coach 救场名额每 run 一次
  - findings 状态机 claimed→verified/rejected；R0 判零在调用尾部；
    R4 oracle 命中→verified(oracle)；claimed 禁报告禁正典
  - Guard：registry `_dispatch` schema 校验后、执行器调用前为唯一拦截点；
    白名单在外/分级在内；HIGH 含 agent_run（FLOOR_HIGH）；denied 回执不进失速计
  - HITL：Approver 注入式（off/prompt/auto），批/拒均落 audit；无审批人 = fail-closed
  - 探针：enable_http_probe 默认关（零开销）；执行窗只罩执行器调用；obs 锚优先
  - 执行隔离：[execute] 段 shell|docker + fail-closed；raw 封面记 ArgvRunner.command
  - 多 worker：MissionBoard 线程池（默认 3）；worker 独立 conn/registry（不含
    agent_run）+ actor=workerA；锚点不假写；approver=None → HIGH fail-closed
  - 面板只读是机制不是约定（ro URI + 全 GET 路由）
- 测试 300 例全绿；ruff（line-length 100、py312）format+check 全绿

## 范围（严格 M4，按依赖顺序）

M4-1  真 LLM provider 接线（R8 风险，接口层唯一 dep）：
      llms/providers/ 下新增 openai 兼容 provider，只实现 complete 原语；
      配置全走环境变量（SECPT_LLM_BASE_URL / SECPT_LLM_API_KEY /
      SECPT_LLM_MODEL），密钥绝不落配置文件/日志/audit；LLM_PROVIDER=mock
      常备且行为不变；缺 key/缺 base_url 显式拒跑（E_CONFIG），不静默降 mock。
      契约测试用本地假 HTTP server（不联网），覆盖：正常/超时退避/schema 带错重试/
      4xx-5xx 分层错误/流式否（MVP 非流式）
M4-2  MCP 工具管道（蓝本 §3.5 kind=mcp 语义兑现）：
      MCP stdio client（list_tools → 注册为 manifest+hash 的 adapter），
      工具调用走 argv/JSON-RPC 双通道但同样禁 shell；连接失败不注册只告警
      （E_MCP 上抛口径与既有错误分层一致）；MCP 工具同权过 Guard/HITL/探针窗；
      fixture 用本地 echo MCP server 演示 list+call；未配置 [mcp] 段 = 零行为变化
M4-3  mitmproxy 抓包后端（§69 图纸位兑现）：
      CaptureBackend 同形替换 ScriptedCapture——真抓包在 TCP PROXY 端口，
      执行窗开合仍只罩执行器调用；meta.mock 消失（真流量如实标注）；
      默认 backend 仍是 mock；mitmproxy 未装/起不来 → E_PROBE 拒跑（fail-closed）；
      R0 obs 锚与判票口径零改动；演示用本地 http server，不碰外网
M4-4  worker 后台审批代理（§71 遗留②）：
      worker 线程内 HIGH 动作不得直读 stdin——经 ApprovalBroker（主线程/队列
      转发，CLI prompt 档由主循环代问）裁决；批/拒两路落 audit（workerA 发起 +
      代理裁决 actor 分层可辨）；未装代理 → fail-closed 保持现语义；
      mock 两路各有账；broker 死锁/超时须有界（超时=拒绝，不悬死）
M4-5  容器加固与执行面收口（§71 遗留④ + 需求"每步工具上限默认为 4"）：
      build_docker_argv 扩展 flags（默认 --network=none、--memory、--read-only、
      --cap-drop=ALL，[execute] 段可配且顺序稳定可测）；容器内无网络需可证；
      主循环每步 tool_calls 超过上限（默认 4，可配）时截断且超限部分落账
      （不进失速计、不静默丢弃）；旧配置/旧脚本行为守恒
M4-6  观察面收口：双图变化事件线 + 两根验收杆度量（§65 遗留 + §10）：
      双图写入后 emit graph_changed（事件线只报告不接管，R3 红线不破），
      面板 SSE 可见；新增只读统计：verified 阳性率（精度杆）与经验命中率
      （复利杆）——纯度量不自动调参，CLI/面板可查，口径写进 docstring

## 四铁律（延续 M1~M3，违反即打回）
1. FSM 管状态 2. 黑板管证据（写入口只经 store/*）
3. Guard 管动作（M4 新增的 MCP/审批代理/容器执行同样过闸）
4. verify 管真伪（verified 必有 judge 落账背书）

## 工程规范（硬性，与 M1~M3 完全一致）
- ruff format + check 全绿提交；完整类型注解（Any 仅限 LLM/MCP 响应边界）
- structlog 库层；错误分层不吞错；argv 列表禁 shell=True；拒控制字符注入
- 密钥只从环境变量读，绝不硬编码/落盘/进日志；外部依赖最小权限
- LLM_PROVIDER=mock + SECPT_PROBE_BACKEND=mock 离线全流程可演示（新器官
  也要有 mock 路径）；任何真外部依赖缺失都必须 fail-closed 而非静默降级
- 测试主战场 = FakeLLM contract test + 预置账本 fixture + 本地假 HTTP/MCP
  server；新增器官必须各有失败注入测试（provider 超时/MCP 掉线/抓包起不来/
  代理超时/容器无网）
- 每张任务卡：先测试→再实现→全绿→提交一次（意图+变更范围+兼容性影响）；
  施工结论续记 harness_design_discussion.md（§72 起）

## 完成定义（M4 验收，全部可证伪）
1. provider：LLM_PROVIDER=openai 兼容档可跑完整的 run（真模型或本地假 server
   演示）；缺 key 显式 E_CONFIG 拒跑；mock 全链 300 例不回退
2. MCP：本地 echo server 注册成功且清单可见，调用落小票；掉线只告警不注册；
   MCP 工具过 Guard 拒绝可证
3. 抓包：enable_http_probe 开 + backend=mitmproxy 时 observations 落真流量
   （meta.mock 不存在），关档零开销；R0 obs 锚判票不回归
4. 审批代理：worker 发起 HIGH 动作批/拒两路各有 audit 账；超时=拒绝不悬死；
   未装代理仍是 fail-closed
5. 容器加固：默认无网可证（容器内 curl 失败且宿主 shim 未跑）；每步工具超限
   截断且落账；旧 shell 档行为逐字节守恒
6. 观察面：graph_changed 事件可见于面板；精度杆/复利杆统计可查且口径有测试
7. pytest 全绿、ruff 全绿；M1/M2/M3 诚实边界不回退（300 例原班不跳红）

## 环境备忘（本机坑）
- 一律 LD_LIBRARY_PATH= .venv/bin/python -m ... 跑 pytest/ruff/CLI
  （系统 python3.13 的 sqlite3 模块对该变量敏感）
- .venv 无 pip，装依赖用：uv pip install --python .venv/bin/python <pkg>
- CLI 演示：SECPT_DB=临时文件 SECPT_NMAP_BIN=占位 LLM_PROVIDER=mock；
  后台起服务必须"起→验→收"单命令完成；shell 里 export 独立语句拟
  （&& 后台链会吃掉赋值）
- 面板：secpt panel --host 127.0.0.1 --port 8711（threaded 只为 SSE）
- 已有镜像：secpt-sandbox / secpt-sandbox-pwn（真镜像冒烟直接可用）
- 不联网也能验收：假 HTTP server（provider）、echo MCP server、本地 http
  server（抓包）、Docker 本地镜像；真实外网只做手工冒烟不进 CI
- 不确定的决策点列成清单一次性提问；不做文档外的"改进"

现在从 M4-1 开工：开工前先核对蓝本 §6.1 LLM 接口层、§3.5 六库分型/MCP 语义、
§7 配置与安全、§10 测试策略；输出 M4-1 的执行计划（文件清单+测试清单+env
契约）确认后动工。
```

## 备注（给粘贴者）

- 该提示词与 M3 版同构：角色 → 唯一依据 → 已交付底盘 → 范围 → 四铁律 →
  工程规范 → 可证伪验收 → 环境备忘 → 开工要求，便于模型直接进入执行态。
- 六张卡按依赖排序：provider 是一切器官的前提；MCP/抓包可并行；审批代理
  依赖 M3-5 的 worker；容器加固与每步上限同属"执行面"；观察面最后收口。
- 如需缩短，可只保留 M4-1/M4-2/M4-3 三卡（真世界三件套），把 M4-4~M4-6
  留给 M4.5；但完成定义的 7 条需同步删减，不可留空。
