# secpt 架构与命名规范（权威）

> 本文档是层级职责与智能体职名的**单点事实源**。代码、文档、日志、Web 界面
> 一律使用本表的规范名；新增模块必须先在此登记职名再加入编排队。
> 实现细节的沿革见 `docs/架构现状.md`。

## 一、四层架构（规范性命名）

| 层 | 规范名 | 职责边界 | 载体模块 |
|---|---|---|---|
| **L0 接入层** | **Gateway** | 任务接入与鉴权：自然语言/附件入口、上传净化、token 认证、scope 预检、**协议闸口**（唯一保留的分类决策：benchmark 平台协议规则检测）。角色/安全场景/经验的选择不是协议，归指挥官 | `cli/solve.py`、`web/api.py`、`cli/app.py`、`core/orchestrator/dag.py` |
| **L1 编导层** | **Command** | 任务池治理、规划-执行-调度、值守触发、快判分流、卡壳介入、交付判定 | `core/orchestrator/frontier.py`、`watch.py`、`commander.py`、`modes.py`（编排模式唯一裁决）、`global_lease.py`（跨任务目标租约）、`triage.py`、`coach.py`、`delivery_gate.py`、`hypothesis.py` |
| **L2 作业层** | **Field** | 单链 ReAct 执行：工具调用、止损三档、compaction、进度锚点、断点续跑 | `runtime/engine.py`、`runtime/local_runner.py`、`runtime/stuck.py` |
| **L3 支撑层** | **Sustainment** | 治理与记忆：scope/限速/拦截/审计、图记忆、findings、经验、评判、证据、产物压缩归档 | `governance/`、`intel/`、`learning/`、`evaluation/`、`findings/`、`evidence/`、`core/sustain/`、`web/`（观测面） |

层间纪律：L0 不做规划决策；L1 不直接执行工具（Execute 阶段的自身执行也经 L2）；
L2 不感知其他执行体（协作只经 L1 的黑板）；L3 只被上三层消费，不反向调用。

## 二、智能体职名（六个 + 一门控）

| 职名 | 缩写 | 载体 | 性质 | 一句话职责 |
|---|---|---|---|---|
| **Commander 指挥官** | CDR | `commander.py` | LLM，事件驱动无会话状态 | 读确定性简报 → 信念结算 → 方向决策 → 派单 → 全量重写战略文档；自身执行也经 L2 |
| **Sentinel 值守哨兵** | SNT | `watch.py` + frontier 结算 | 纯代码，零 LLM，常驻 | 结算一切事件；四类确定性触发（预算/停滞/阶段门/内务）唤醒 Commander |
| **Operator 操作员** | OPT | `runtime/engine.py` 实例 | LLM，ReAct，任务内全自主 | 多实例并行（操作员A/B/C…）：领取互相独立的任务，Thought→Action→Observation 直到完成；耗时粗活委托后勤 |
| **Quartermaster 军需官** | QM | logistics lane | LLM，队列消费者 | 后台承接下载/批量搜索/爆破/爬取等耗时工作，只回写不决策 |
| **Verifier 核验官** | VRF | `delivery_gate.py` | 确定性 + 按需 LLM | 质量工程师：证据核验与独立复现，只评交付物质量不拍板——否决经 quality 信号穿透交付等待送达 CDR 综合裁决（修复/坚持/转向）；复验任务入池推进尝试计数，耗尽仍未过 → 信念回滚 + 按未验证收口 |
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
| events.jsonl（事件流，seq 单调） | `var/jobs/<id>/` | 全体（SNT 聚合子流；父流三写入方持 PEV_LOCK 串行） | Web 面板 / 评判 / 复盘 |
| findings.sqlite + 证据 blob | `var/` | OPT 上报 / VRF 复验 | 报告 / 交付 |
| benchmark_ledger.sqlite | `var/` | campaign 认领与结算 | 跑分防双开 |
| 技能目录（`SKILL.md` + frontmatter 元数据） | `skills/` + `SECPT_SKILLS_DIRS` + `var/skills/`（后写覆盖先写） | 用户/部署方投放 | CDR 目录选挂载（派单 `skills`）→ OPT 目标注入【技能包】 |
| 全局目标租约（O_EXCL 文件 + TTL，advisory） | `var/leases/` | 各 engagement 入口抢租/收口释放 | 并行任务同目标互撞避让（告警不硬阻断） |
| 归档事件（`events-*.jsonl.gz` + `watermark.json`） | `var/jobs/<base>/archive/` | `core/sustain/compress` 收尾轮转 | Web 事件流游标回读补归档段 |

协作纪律：智能体间不直接对话；一切经黑板交换；认领走 BEGIN IMMEDIATE 原子
事务 + 资产租约；事件流 append-only。

## 四、一次任务的规范时序

```
Gateway 接入（自然语言 + 附件；协议闸口仅检测 benchmark 平台协议——规则命中即战役跑分）
  → CDR 首轮唤醒（任务理解 + 角色赋予）：
        理解客户原始需求 → 产 GoalSpec（落盘 goal.json）
        角色/安全场景选择（从角色目录选一，落盘 role.json）+ 经验选择（priors）
        假设树 v0 + 战略文档 v0 + 拆分派首批任务
        派单三要素：max_wall_s（何时截止）+ notes（注意事项：边界/风险/禁止动作）
          + self（简单任务 CDR 当场自执行，不占操作员名额）
        派单可逐任务带 profile/prior——同一任务的不同操作员可领不同角色
        （如 web+pwn 混合任务）；缺省沿用任务级角色与经验
        派单可逐任务带 skills（技能目录挂载）——按任务把 SKILL.md 正文注入操作员目标
  → 稳态循环（占 90%）：操作员A/B/C…并行领单 ReAct ──产出──→ SNT 结算
        │ 任务状态全程落在作战地图（frontier.sqlite / hypothesis.sqlite / strategy.md / events.jsonl）
        │ 无触发器：循环继续，CDR 不在场
        │ 质变（confirmed 假设/真实 flag/本 engagement 新 findings）：当轮插队唤醒 CDR 读图重估
        │ 有触发器：唤醒 CDR（信念结算→随时调整方向→再派单→改文档→休眠）
        │   多信号同轮攒下时一次唤醒全交付（列表化简报，防信号饿死）；
        │   简报携态势：已确认战果 + 在途任务（谁还在场上/跑了多久）+ 历史经验命中率 Top-3
        │ 操作员目标编译注入：主攻方向（战略文档摘录）+ 已验证漏洞跨资产情报 + 技能包
  → 卡壳：SNT 唤醒 COH → 诊断 → 要求 CDR 重写战略文档 → COH 退出
  → 收敛：SNT 只递 convergence 信号不拍板 → CDR 读图裁决（deliver 或补产）；
        CDR 连续两次不交付 → SNT 回退兜底收口
  → 收敛：CDR 请求交付 → SNT 排空在途后勤 → VRF 核验（质量工程师，只评不拍板）
        → 通过：结束（verified_flags 落账）；否决：quality 信号穿透交付等待送达
          CDR 综合裁决（修复/坚持/转向），复验任务入池推进尝试计数——耗尽仍未过
          → confirmed 信念回滚（置信度×0.6 + [rollback] 证据），按未验证收口
  → 终局：SNT 写 retro.md，学习层蒸馏经验；sustain.compress 轮转归档 + VACUUM + 日志打包
```
