# secpt 架构与命名规范（权威）

> 本文档是层级职责与智能体职名的**单点事实源**。代码、文档、日志、Web 界面
> 一律使用本表的规范名；新增模块必须先在此登记职名再加入编排队。
> 实现细节的沿革见 `docs/架构现状.md`。

## 一、四层架构（规范性命名）

| 层 | 规范名 | 职责边界 | 载体模块 |
|---|---|---|---|
| **L0 接入层** | **Gateway** | 任务接入与鉴权：自然语言/附件入口、上传净化、token 认证、scope 预检、profile 路由 | `cli/solve.py`、`web/api.py`、`cli/app.py`、`core/orchestrator/dag.py` |
| **L1 编导层** | **Command** | 任务池治理、规划-执行-调度、值守触发、快判分流、卡壳介入、交付判定 | `core/orchestrator/frontier.py`、`watch.py`、`commander.py`、`triage.py`、`coach.py`、`delivery_gate.py`、`hypothesis.py` |
| **L2 作业层** | **Field** | 单链 ReAct 执行：工具调用、止损三档、compaction、进度锚点、断点续跑 | `runtime/engine.py`、`runtime/local_runner.py`、`runtime/stuck.py` |
| **L3 支撑层** | **Sustainment** | 治理与记忆：scope/限速/拦截/审计、图记忆、findings、经验、评判、证据 | `governance/`、`intel/`、`learning/`、`evaluation/`、`findings/`、`evidence/`、`web/`（观测面） |

层间纪律：L0 不做规划决策；L1 不直接执行工具（Execute 阶段的自身执行也经 L2）；
L2 不感知其他执行体（协作只经 L1 的黑板）；L3 只被上三层消费，不反向调用。

## 二、智能体职名（六个 + 一门控）

| 职名 | 缩写 | 载体 | 性质 | 一句话职责 |
|---|---|---|---|---|
| **Commander 指挥官** | CDR | `commander.py` | LLM，事件驱动无会话状态 | 读确定性简报 → 信念结算 → 方向决策 → 派单 → 全量重写战略文档；自身执行也经 L2 |
| **Sentinel 值守哨兵** | SNT | `watch.py` + frontier 结算 | 纯代码，零 LLM，常驻 | 结算一切事件；四类确定性触发（预算/停滞/阶段门/内务）唤醒 Commander |
| **Operator 作业智能体** | OPT | `runtime/engine.py` 实例 | LLM，ReAct，任务内全自主 | 领取独立任务，Thought→Action→Observation 直到完成；耗时粗活委托后勤 |
| **Quartermaster 军需官** | QM | logistics lane | LLM，队列消费者 | 后台承接下载/批量搜索/爆破/爬取等耗时工作，只回写不决策 |
| **Verifier 核验官** | VRF | `delivery_gate.py` | 确定性 + 按需 LLM | 证据核验与独立复现；裁决 confirmed/inferred/rejected；判定整体任务是否结束 |
| **Coach 教练** | COH | `coach.py` | 确定性，休眠唤醒 | 仅对卡壳信号介入：要求 Commander 重写战略文档（不塞建议） |
| **Gate 门控** | — | `triage.py` + `governance/` | 确定性前置，模型仅歧义兜底 | 拦界不判优：scope/限速/危险命令/任务分级与分流 |

命名纪律：
- 事件流 `actor` 字段使用缩写小写：`commander` / `sentinel` / `operator` /
  `quartermaster` / `verifier` / `coach`；作业事件附带 job_id 定位到具体实例。
- Web 面板徽标展示「职名 · 实例 id」；文档首次出现用全称，后文用缩写。
- 不使用"主智能体 A/B/C、小助手、机器人"等口语称呼；角色与实例分层表述
  （职名是岗位，job_id 是班次）。

## 三、信息同步载体（黑板）

| 载体 | 路径 | 写者 | 读者 |
|---|---|---|---|
| frontier.sqlite（任务池 + 资产租约） | `var/jobs/<base>/` | CDR 派单 / OPT 领取结算 / SNT 修剪 | 全体 |
| hypothesis.sqlite（假设树） | `var/jobs/<base>/` | CDR 结算 / OPT 证据事件 | CDR / Verifier |
| strategy.md（作战意图） | `var/jobs/<base>/` | CDR 全量重写 | CDR 下次唤醒 / Verifier |
| events.jsonl（事件流，seq 单调） | `var/jobs/<id>/` | 全体（SNT 聚合子流） | Web 面板 / 评判 / 复盘 |
| findings.sqlite + 证据 blob | `var/` | OPT 上报 / VRF 复验 | 报告 / 交付 |
| benchmark_ledger.sqlite | `var/` | campaign 认领与结算 | 跑分防双开 |

协作纪律：智能体间不直接对话；一切经黑板交换；认领走 BEGIN IMMEDIATE 原子
事务 + 资产租约；事件流 append-only。

## 四、一次任务的规范时序

```
Gateway 接入（自然语言 + 附件）
  → CDR 首次唤醒：假设树 v0 + 战略文档 v0 + 派首批任务
  → 稳态循环（占 90%）：OPT 领单 ReAct ──产出──→ SNT 结算
        │ 无触发器：循环继续，CDR 不在场
        │ 有触发器：唤醒 CDR（信念结算→决策→派单→改文档→休眠）
  → 卡壳：SNT 唤醒 COH → 诊断 → 要求 CDR 重写战略文档 → COH 退出
  → 收敛：CDR 请求交付 → SNT 排空在途后勤 → VRF 核验
        → 通过：结束（verified_flags 落账）；否决：退回任务池
  → 终局：SNT 写 retro.md，学习层蒸馏经验
```
