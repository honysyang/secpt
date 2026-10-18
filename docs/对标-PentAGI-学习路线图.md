# 对标 PentAGI · 学习与优化路线图（2026-10-17）

> 视角声明：PentAGI（vxcontrol/pentagi）是"带 UI 的通用智能体框架"，secpt 是"带验证
> 纪律的闭环引擎"——学习目标不是变成它，而是**抄走它在小模型驱动上用实测验证过的
> 机制**（它与 secpt 同样以 Qwen/DeepSeek 级模型跑主力，结论可直接迁移）。
> 五样 secpt 已反超、禁止倒退的：对抗式复现验证纪律、内容寻址证据链、评测四层、
> 意图 fact 依赖、三档止损上下文工程。

## 现状对照（PentAGI 能力 → secpt 现状 → 差距）

| PentAGI 机制 | secpt 现状 | 差距定性 |
|---|---|---|
| Execution Monitoring + adviser 导师介入（实测 2x 质量 / 2-3x token，<32B 模型必需） | stuck 四判据 + 固定文案 nudge | **机制有，智能无**——最值得抄 |
| 记忆四型（memory/guide/answer/code，显式生命周期） | experiences.jsonl 扁平一锅 | 分型缺失 |
| etester（embedding test/info/flush/reindex） | doctor 无 embedding 体检；fastembed 挂过全场没人知道 | 已踩过的坑，补上 |
| ctester（角色×模型 function-calling 实测选型） | VERIFY/ESCALATE 两个口子，选型凭感觉 | 缺数据工具 |
| Sploitus exploit/CVE 检索 | web_search 只有通用引擎（Tavily/Brave/SearXNG） | c 族"指纹→CVE"缺专用出口 |
| Reflector 优雅终止 + 按角色工具硬上限（100/20） | tool-call JSON 解析失败硬扛；max_steps 不分角色 | 小改 |
| Langfuse/OTel trace 可查 | events.jsonl 数据全有，无查看器 | 抄思想不抄全家桶 |
| 微服务全家桶 / Flow-Task-SubTask 三层 / BAS | SQLite 零依赖 / 单层意图+fact 依赖（更强）/ 未做 | **反目标，不抄** |

## Phase PA1 · 可信基础设施（治已知的坑，~1 天）

- **PA1.1 embedding 体检进 doctor**（etester 思想）：新增检查项——模型可加载、
  向量维度与缓存一致、降级时告警"当前为字面检索"（1036 教训：降级了 8 小时无人知）
- **PA1.2 `secpt learn --reindex`**：embed-cache.json 重建（换模型/清缓存用）
- **PA1.3 Sploitus 后端**：web_search 插件加 exploit/CVE 专用检索（免费 API），
  `_WEBSEARCH_HINTS` 命中即有出口——c 族"指纹→已知漏洞"最后一块拼图

**验收**：`secpt doctor` 显示 embedding 真实状态与原因；对 "HugeGraph 1.2.0"
检索返回 exploit 候选列表；FakeLLM 可测。

## Phase PA2 · Mentor 止损升级（杠杆最大，3-5 天）

- `should_nudge` 触发时先调**导师 LLM**（`SECPT_ESCALATE_MODEL`，未配则降级
  现有固定文案——best-effort 纪律不破）：输入=最近 10 步结构化摘要 + 否定清单 +
  卡点指纹，输出=换路线建议，注入为 user 消息
- 成本护栏：每意图 mentor 调用 ≤2 次、tokens 记账、失败静默降级
- PentAGI 实测口径（2x 质量 / 2-3x token）决定了它只开在"止损决策点"，不全量开

**验收**：1323 的 17 道残题轨迹回放，人工评 mentor 建议命中率 ≥50%；
线上 nudge→wrap 转化率（空转烧完预算的比例）显著下降。

## Phase PA3 · 按角色测模型（ctester 思想，2-3 天）

- `secpt ctest --role <role> --model <model>`：标准微任务集（JSON 意图生成 /
  tool-call 格式合规 / 旗验证判断 / 反作弊判断）→ 成功率报告
- 产出"角色 × 模型"选型表，反哺 per-role 模型配置（把 VERIFY/ESCALATE 扩成
  DEFAULT_ROLE_MODELS 表）
- 框架本身用 FakeLLM 地基，无 key 可测

**验收**：5 角色 × ≥2 模型报告各一份；至少一项选型变化由报告驱动落地。

## Phase PA4 · 终止与上限精细化（~2 天）

- tool-call JSON 解析连续失败 ≥2 次 → 注入自救提示（"改用 write_file 写脚本
  输出 JSON"），再失败才走既有 error 路径（Reflector 思想）
- 意图级工具调用硬上限按角色分档（exploit 100 / verify·report 20），超限走
  既有 wrapping——防单角色烧穿预算

**验收**：解析失败自救路径单测；hard/exploit 角色不受新上限误伤。

## Phase PA5 · 记忆分型（3-4 天）

- 经验条目加 `kind`：`trace`（执行记忆，默认）/ `playbook`（可复用打法）/
  `guide`（方法论手册）；retrieve 按 kind 加权（playbook > trace）
- distill 写入端标注 kind；存量迁移脚本（默认 trace，幂等）
- 与 playbook.py 分工对齐（playbook=族级实战蒸馏，guide=通用方法论）

**验收**：检索 top-3 中 playbook 类优先命中；1323/1036 存量迁移后 recall 不回归。

## Phase PA6 · 观测最小版（1-2 天）

- `secpt watch --timeline <job_id>`：events.jsonl → 每步表格
  （步号/耗时/工具/token/loop_phase/stuck 信号）——Langfuse 思想的零依赖版
- 不引入 Langfuse/OTel/Grafana 任何外部服务

**验收**：对 1323 历史任务一条命令出完整时间线；输出可 grep。

## 依赖与顺序

```
PA1（独立，先做——治已踩的坑）──→ PA2（导师需要模型档位概念，但可降级）
PA3（独立）──→ 反哺 PA2 的导师模型选型
PA4/PA5/PA6 相互独立，随插随做
```

推荐节奏：PA1 → PA4 → PA2 → PA3 → PA5 → PA6（价值/成本比排序；
PA1/PA4 合计 ~2 天，先吃掉两个确定性收益）。

## 反目标（明确不抄）

- ❌ 微服务全家桶（Postgres/ClickHouse/Redis/MinIO/Neo4j/Kafka）——SQLite 零依赖
  是单操作者场景的正确选择
- ❌ Flow/Task/SubTask 三层结构——单层意图 + `fact:` 依赖 + 资产租约表达力更强
- ❌ BAS（浏览器内攻击模拟）——PentAGI 自己都没实现

## 通用验收纪律（沿用项目约定）

新功能必带单测且 FakeLLM 可测；`make check` 全绿；每 Phase 结束在真实靶场跑
一场并用数据回看（1036 已沉淀四指标：scope.denied 归零 / request_tools 使用 /
probe.py 触发 / 409 步数归零——本路线图各 Phase 验收沿用同口径）。
