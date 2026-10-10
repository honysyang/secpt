# secpt

自主渗透/复杂任务 Agent harness。Python 3.12 + asyncio + pydantic-settings + structlog + typer。

> 当前架构以 `docs/架构现状.md` 为准（`secpt-开发文档.md` v0.3 为历史文档）。

## 快速开始（推荐主入口 solve）

```bash
export DEEPSEEK_API_KEY=<key>
secpt solve challenges/rois-saml-sso          # 一键：入口/附件/先验全自动 → 运行 → 评判 → 学习
secpt solve <dir> --prior saml --wall 1800    # 覆盖项仍可干预
secpt solve <dir> -p multistage               # 多阶段：recon → getshell → privesc
```

## 核心能力

- **一键解题（solve）**：入口 URL 抽取、附件自动解压、先验关键词匹配（`knowledge/prior_map.yaml` 可扩展）、跑完自动评判 + 经验沉淀
- **越用越好用（learning 闭环）**：每次运行 LLM 蒸馏经验入 `var/knowledge/experiences.jsonl`，下次同类任务自动注入 top-3 历史经验（`SECPT_EXPERIENCE=0` 关闭）
- **工具插件（tools/plugins/）**：YAML 声明式集成外部工具/其他智能体能力（如 getshell），模板参数 shlex.quote 防注入；profile `tools:` 声明即装配
- **多阶段渗透（profile stages）**：recon → getshell → privesc 串行编排，共享 workspace，阶段交付物自动交接
- **AI 评判（不依赖 flag 正则）**：`secpt judge <job_id> [--ground-truth FILE]`——反作弊 → Jev 门控 → LLM judge 终裁
- **7×24 长任务**：`--wall 0` 不限墙钟，compaction(0.72 批量)/rollover 上下文自愈，协作式取消
- **多任务**：`secpt submit -t spec.yaml -j 4` / `secpt forever`（进程池 + 优先级队列）
- **多模型**：`--model openai/gpt-4o` 或 `SECPT_MODEL_BASE_<PREFIX>` 接入任意 OpenAI 兼容端点

## 插件示例（集成 getshell 能力）

```yaml
# tools/plugins/getshell.yaml
name: getshell
description: 对目标执行 getshell 尝试
schema: {type: object, properties: {target: {type: string}}, required: [target]}
run: "getshell --target {target} --json"   # 换成你环境里真实的工具 CLI
timeout_s: 300
```

## Phase 0 目标

1. 目录骨架与命名约定（仅骨架，不含业务实现）
2. 配置约定：`config.yaml` + `.env`（pydantic-settings，env 优先）
3. 关键技术栈的占位模块：
   - `core/react` —— 自研 ReAct 循环
   - `core/graph` —— GraphStore 三档后端（内存 → SQLite → Postgres）
   - `core/llm` —— litellm 适配，DeepSeek 透传
   - `tools` —— 白名单 subprocess 侦察工具
   - `skills` —— 可复用技能 / 角色
   - `cli` —— typer CLI 入口
   - `telemetry` —— structlog 结构化日志
   - `tests` —— pytest（含 pytest-asyncio）

## 快速开始（Phase 0 占位）

```bash
# 1. 准备环境
cp .env.example .env          # 写入 DEEPSEEK_API_KEY
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. CLI 入口（占位）
secpt --help
secpt doctor                 # 检查环境/配置
secpt run --target example.com   # Phase 1+ 才会真正执行

# 3. 测试
pytest

# 4. 复跑判据门禁（每次实战复跑后必跑）
make recheck REPORT=data/reports/run.xxx.json
```

## 配置优先级

```
环境变量 (SECPT_xxx__yyy)  >  config.yaml  >  pydantic 模型默认值
```

例：环境变量 `SECPT_LLM__MODEL=deepseek-reasoner` 会覆盖 `config.yaml` 里的 `llm.model`。

## 目录约定

```
secpt/
├── pyproject.toml
├── config.yaml
├── .env.example
├── src/secpt/
│   ├── __init__.py
│   ├── cli/
│   │   └── app.py             # typer 入口（secpt 命令）
│   ├── config/
│   │   ├── settings.py        # pydantic-settings 根配置
│   │   └── models.py          # 子配置模型（LLM / Graph / React / Tools / Logging / Audit）
│   ├── core/
│   │   ├── react/             # 自研 ReAct 循环（think → act → observe）
│   │   ├── graph/             # GraphStore 抽象 + memory/sqlite/postgres 三档实现
│   │   ├── llm/               # litellm 适配，DeepSeek 透传
│   │   └── tools/             # 工具注册中心（白名单拦截）
│   ├── tools/
│   │   ├── recon/             # 侦察类：nmap / curl / httpx / dig / whois
│   │   └── web/               # web 类：phase 1+ 起步
│   ├── skills/                # 可复用技能 / 角色（role-based）
│   ├── telemetry/
│   │   └── logging.py         # structlog 配置（JSON / console）
│   ├── audit/                 # 操作审计日志（落盘不可变）
│   └── runtime/
│       ├── runner.py          # 顶层 run 编排
│       └── subprocess.py      # 白名单 subprocess 封装
├── tests/
│   ├── conftest.py
│   ├── unit/
│   └── integration/
└── data/                      # 运行时产物（gitignored）
```
