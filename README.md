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

## 快速开始（唯一入口 solve）

```bash
# 1. 准备环境
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
export DEEPSEEK_API_KEY=<key>          # 或任意 OpenAI 兼容端点（见 docs/架构现状.md 模型路由）

# 2. 一键执行——任务类型与打法都不需要用户判断：
secpt solve challenges/rois-saml-sso
#    内部自动：profile 路由（规则关键词→LLM 兜底）→ 先验匹配 → 执行模式
#    （懒升级阶梯：single → 无成果自动升级 frontier → stages 若 profile 编排）
#    → AI 评判 → 经验沉淀。高级用户可用 -p/--prior/--mode 覆盖任何一步。

# 3. 无 key 干跑（FakeLLM 全链路）
SECPT_FAKE_LLM=1 secpt solve challenges/rois-saml-sso

# 实时观察（另开终端）：逐步思考/工具调用/响应/发现/压缩
secpt watch <job_id>

# 4. 测试与规范
make check                             # ruff check + ruff format --check + pytest（提交前必跑）
make recheck REPORT=<report.json>      # 可选：复跑判据门禁
```

> 🚀 **想快速读懂整个系统？→ [docs/系统速览.md](docs/系统速览.md)**（5 分钟版）
> 开发约定与架构红线见 [AGENTS.md](AGENTS.md)；文档导航见 [docs/README.md](docs/README.md)；
> 权威架构描述见 [docs/架构现状.md](docs/架构现状.md)。

## 配置优先级

```
环境变量 (SECPT_xxx__yyy)  >  config.yaml  >  pydantic 模型默认值
```

例：环境变量 `SECPT_LLM__MODEL=deepseek-reasoner` 会覆盖 `config.yaml` 里的 `llm.model`。

## 目录约定

```
secpt/
├── AGENTS.md                  # 项目约定（构建/测试/风格/红线）——修改前必读
├── Makefile                   # make check = lint + format-check + test
├── pyproject.toml             # ruff(line-length 100, py312) / pytest / coverage
├── src/secpt/
│   ├── cli/                   # typer 薄注册层：solve(★主入口) / watch / cmd_run / cmd_findings / cmd_jobs
│   ├── runtime/               # engine.py(★主循环) / llm client(多模型路由+池) / sandbox(Docker沙箱)
│   ├── core/
│   │   ├── job/               # 作业层：TaskSpec/JobRecord/store/event/manager/worker
│   │   ├── orchestrator/      # dag 分流(默认单链) / stages 多阶段 / frontier 意图调度
│   │   ├── profile/           # profile YAML loader + system_extra 渲染
│   │   ├── context/           # S3 四件套：compaction / rollover / budget / summarize
│   │   ├── memory/            # warroom 共享作战记忆（作业内暂存）
│   │   └── watchdog.py        # 空转监控
│   ├── evaluation/            # AI 评判层（anticheat→Jev→judge→verdict）
│   ├── findings/              # 漏洞发现全生命周期（验证纪律/CWE/CVE 去重）
│   ├── intel/                 # 图记忆（资产图+血缘图+覆盖度+冷压缩）
│   ├── traffic/ + evidence/   # MITM 流量录制 + 内容寻址证据快照
│   ├── report/                # CWE 映射 + 报告交付（md/md-zip 证据包）
│   ├── learning/              # 经验闭环（distill / store / retrieve）
│   ├── tools/                 # plugin_loader（plugins/ 外部能力集成）
│   ├── governance/            # scope / ratelimit / risk / audit
│   ├── challenge/             # 挑战加载（题面/入口/附件，solve 的原材料）
│   └── knowledge/             # priors/ 先验打法 + prior_map.yaml 路由表
├── tests/unit/**              # 255 个单测（FakeLLM 为地基，无 key 全链路可测）
├── tools/plugins/             # 用户插件（YAML 声明式 / Python）
├── docs/                      # 架构现状(权威) + 索引 + 实验报告
├── var/                       # 运行时数据（jobs/knowledge/audit…，gitignored）
└── archive/                   # 历史资料（v0.3 文档/旧格式数据），仅供考古
```
