# AGENTS.md — secpt 项目约定

> 面向编码智能体与人类贡献者。修改代码前必读；
> 修改本文档所述内容时**必须同步更新本文档**。

## 项目一句话

secpt 是自主渗透/复杂任务 Agent harness：通用任务引擎 + 安全场景增强 +
AI 评判层 + 经验闭环。**当前架构以 `docs/架构现状.md` 为权威描述**
（`docs/archive-开发文档-v0.3.md` 是已删除架构的历史文档，勿按其找代码）。

## 构建 / 测试 / 运行

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

make test        # = pytest tests/ -q（2026-10-17 口径：收集 852；全绿+4 可跳过=完成）
make lint        # ruff check（必须全绿才算完成）
make format      # ruff format
make format-check  # ruff format --check（只查不改，与 CI 一致）
make check       # lint + format-check + test（提交前必跑）

# 无 key 干跑（ FakeLLM，全链路可测 ）
SECPT_FAKE_LLM=1 python -m secpt.cli.app solve challenges/rois-saml-sso

# 真实运行
export DEEPSEEK_API_KEY=<key>
secpt solve challenges/rois-saml-sso
```

## 代码风格（由 ruff 强制，CI 阻断）

- line-length 100，target py312；`make lint` / `make format` 即全部规范
- 中文 docstring/注释中的全角标点（；，。）是**有意使用**（RUF001/2/3 已豁免）
- `(str, Enum)` 双继承是有意为之（JSON 序列化稳定，UP042 已豁免），勿改 StrEnum
- 库级代码（engine/worker 等）新代码用 structlog；历史 `print(..., file=sys.stderr)`
  渐进迁移，不强制一次改完
- 类型注解：新代码必填；`Any` 仅在边界（LLM 响应等）使用

## 架构红线（改这些地方前先读对应模块 docstring）

1. **messages append-only**：`runtime/engine.py` 主循环四条语义（整条 append /
   assistant 原样 / tool 结果回灌同链 / ANSWER 自宣告）。唯一允许删改重建的点是
   compaction 与 rollover
2. **禁 shell=True**：bash 工具与插件一律 argv 数组执行；白名单首 token +
   未引用 shell 元字符检查（引号内元字符是合法参数）
3. **数据驱动**：异构场景用 profile YAML / stage YAML / 插件 YAML 表达，
   不加新执行器类；靶场相关的任何逻辑不得硬编码进引擎
4. **已删除组件不得复活**：core/evaluator、core/node 等见
   `core/orchestrator/policy.py` DEPRECATED_MODULES
5. **学习/评判是 best-effort**：learning 与 evaluation 任何失败静默降级，
   绝不让观测层打断主链路
6. **审批/操舵是硬机制**：拦截规则求值在 run_bash 执行前（SECPT_INTERCEPT=0 可关）；
   steer 经 SQLite 跨进程注入（hooks.on_step 返回字符串约定）

## 目录速查

```
src/secpt/
  cli/           typer 入口（app=薄注册层）+ solve(★主入口) / watch / cmd_run / cmd_findings / cmd_jobs
                 / cmd_ops(intent/steer/审批/worker) / cmd_doctor(预检) / tui_app+tui_data(面板)
  runtime/       engine.py(★主循环) / llm/client(多模型路由+池) / sandbox(Docker沙箱) / stuck(卡死检测)
                 / notify(通知通道)
  core/
    job/         作业层：TaskSpec/JobRecord/store/event/resume/rounds/manager/worker
    orchestrator/ dag 统一路由(benchmark→campaign；其余→frontier) / frontier 意图调度
                (planner 产独立通用任务；triage/JEV 快判 main/logistics；stages 折叠线性链；
                 main+logistics 双通道；交付门 delivery_gate；教练 coach 注入；retro.md)
                triage.py 快判 / coach.py 卡壳纠偏 / delivery_gate.py 交付核验与结束判定
                注：roles.py 五角色模板是 legacy（SECPT_ROLE_TEMPLATES=1）；
                SpecialistOrchestrator 与 run_stages/run_single_chain 已退役（S6 模式收敛，
                见 policy.DEPRECATED_MODULES），勿复活——唯一执行编排是 frontier；
                WarroomHooks 在 core/memory/warroom.py
                v4：hypothesis.py 假设树（世界模型）/ watch.py 值守层（失败类型化+触发器，零 LLM）/
                commander.py 无状态主智能体（战略文档+防漂移闸+简报编译器）；收敛看假设树非池空
                v3 开关：SECPT_ORCH_LOGISTICS=0 关后勤 / SECPT_ORCH_DELIVERY=0 关交付门 /
                SECPT_ORCH_COMMANDER=0 回退 v3 planner /
                SECPT_ORCH_JEV=1(+JEV_API_KEY) 让外部 typed JEV 参与快判 /
                SECPT_ROLE_TEMPLATES=1 恢复 legacy 角色模板（默认关闭）
    profile/     profile YAML loader + render（system_extra 渲染）
    context/     S3 四件套：estimator/compaction(0.72)/roundpool(0.95)/budget/summarize
    memory/      warroom 四表（历史/可选：无生产装配点——多意图协作走 intel 图 + findings + traces）
    watchdog.py  空转监控（连续回合零新增 → paused + 换层提示）
  evaluation/    AI 评判层：anticheat → ground_truth → Jev 门控 → judge → verdict
  findings/      漏洞发现全生命周期：模型/存储/report_finding 工具/verify 对抗式复现/cve 去重
  intel/         图记忆：资产图+探索血缘图+锚点覆盖度+联想召回+冷压缩
  web/           Web 控制台：server.py(stdlib HTTP+静态) / api.py(payload 构造+任务注册表) /
                 static/(index/app.js/style.css，零构建)。数据全部读既有单点事实源
                 （events.jsonl seq 游标 / intel / findings / experiences / frontier+假设树）；
                 任务提交=后台线程 dag.run_task（fake 模式传 FakeLLM 不污染 env）
  learning/      经验闭环：distill(run后) / store(jsonl,sha1去重) / retrieve(启动注入)
  traffic/       MITM 透明录制（mitmproxy）+ harness（per-job 容器生命周期）
  evidence/      内容寻址证据快照（SHA-256 blob）+ 四角色绑定 + 证据包导出
  report/        CWE 映射（内置字典+幻觉校验）+ 报告交付（md/md-zip/json + 一致性快照）
  tools/         plugin_loader（tools/plugins/*.yaml|py 外部能力集成）
  governance/    scope/ratelimit/risk/audit + intercept.py 拦截审批(ask 阻塞人工放行)
  challenge/     挑战加载（题面/入口抽取/附件解压，solve 的原材料）
  config/        pydantic-settings 配置（app/llm/logging/audit/pricing）
  knowledge/     priors/（先验打法 md）+ prior_map.yaml（关键词→先验路由表）
tests/           单元测试（tests/unit/**，conftest 默认关沙箱）；FakeLLM 为地基，无 key 全链路可测
tools/plugins/   用户插件目录（YAML 声明式 / Python）
docker/          sandbox.Dockerfile（执行沙箱镜像）
docs/            系统速览(第一入口) + 架构现状(权威) + 路线图 + 索引
var/             运行时数据（jobs/knowledge/findings/intel.sqlite…，不入库）
archive/         历史资料（v0.3 文档/旧格式数据），仅供考古
```

## 配置与环境变量

- **环境变量是唯一配置源**（config.yaml 已移除）：项目根 `.env` 自动加载，
  真实 export 优先于 .env；支持 `export ` 前缀与行内注释（`#` 前须有空白）；
  全部可调项见 `.env.example`
- 数据根统一 `var/`（SECPT_VAR 可覆盖）；模型路由 `SECPT_MODEL_BASE_<PREFIX>` /
  `_KEY_<PREFIX>`；惯例变量 `DEEPSEEK_BASE_URL` / `OPENAI_BASE_URL` 作 provider
  base URL 兜底（优先级低于 secpt 专有变量）；上下文窗口 `SECPT_CONTEXT_WINDOW`
- API key 只从环境变量读取，**绝不写入仓库**（含 YAML/测试/fixture）

## 测试约定

- 新功能必须带单测；FakeLLM（`SECPT_FAKE_LLM=1`）是测试策略地基——
  无 key 必须能跑通主链路
- 需要真实 API 的测试用 `pytest.mark.skipif` 缺 key 跳过（保持基线 4 skips）
- 修改 job 状态机 / events 类型 / messages 语义时，先跑
  `tests/unit/core/job/` 与 `tests/unit/runtime/test_engine_smoke.py`
