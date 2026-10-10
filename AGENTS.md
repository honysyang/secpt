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

make test        # = pytest tests/ -q（基线：255 passed / 4 skipped）
make lint        # ruff check（必须全绿才算完成）
make format      # ruff format
make check       # lint + test（提交前必跑）

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

## 目录速查

```
src/secpt/
  cli/           typer 入口（solve=主入口 / run / judge / learn / chat / jobs…）
  runtime/       Engine 主循环（engine.py ★）、LLM client（多模型路由）、intake
  core/
    job/         作业层：TaskSpec/JobRecord/store/event/resume/rounds/manager/worker
    orchestrator/ dag 分流（默认单链+重试）、stages 多阶段、specialist(DAG,默认关)
    profile/     profile YAML loader + render（system_extra 渲染）
    context/     S3 四件套：estimator/compaction(0.72)/roundpool(0.95)/budget/summarize
    memory/      warroom 共享作战记忆（SQLite 四表）
    llm/         litellm 适配（chat/intake 路径）
  evaluation/    AI 评判层：anticheat → ground_truth → Jev 门控 → judge → verdict
  learning/      经验闭环：distill（run后）/store(jsonl,sha1去重）/retrieve(启动注入)
  tools/         内置工具 + plugin_loader（tools/plugins/*.yaml|py 外部能力集成）
  governance/    scope/ratelimit/risk/audit
  knowledge/     priors/（先验 md）+ prior_map.yaml（solve 关键词匹配表）
tests/           单元测试为主（tests/unit/**），1 个集成文件（本地 skip）
tools/plugins/   用户插件目录（getshell.yaml 示例）
var/             运行时数据（jobs/knowledge/audit/sessions…，不入库）
archive/         历史资料（v0.3 文档、旧格式 runs、data-legacy），仅供考古
docs/            架构现状.md(权威) + 索引 README.md + 实验报告
```

## 配置与环境变量

- 配置优先级：环境变量 `SECPT_xxx__yyy` > `config.yaml` > pydantic 默认值
- 数据根统一 `var/`（SECPT_VAR 可覆盖）；模型路由 `SECPT_MODEL_BASE_<PREFIX>` /
  `_KEY_<PREFIX>`；上下文窗口 `SECPT_CONTEXT_WINDOW`
- API key 只从环境变量读取，**绝不写入仓库**（含 YAML/测试/fixture）

## 测试约定

- 新功能必须带单测；FakeLLM（`SECPT_FAKE_LLM=1`）是测试策略地基——
  无 key 必须能跑通主链路
- 需要真实 API 的测试用 `pytest.mark.skipif` 缺 key 跳过（保持基线 4 skips）
- 修改 job 状态机 / events 类型 / messages 语义时，先跑
  `tests/unit/core/job/` 与 `tests/unit/runtime/test_engine_smoke.py`
