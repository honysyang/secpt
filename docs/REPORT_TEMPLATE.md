# secpt 报告模板（客户版）

> 这是 `report.md` 的目标形态。原则：**先给状态，再给验证结论，再给证据；不能用“同族有线索”冒充“预期值已验证”。**

```markdown
> STATUS: PARTIAL / CONTRACT_CONFLICT
> 本报告不是一次“无发现即无风险”的干净扫描；请先阅读验收核对与未验证项。

# <目标> 安全评估报告

## 结论摘要

- 已验证：acc-1、acc-4
- 验收预期与证据冲突：acc-2、acc-3（不是单纯的“未达成”）
- 未观察/未验证：acc-5、acc-6
- 发现：0 条 confirmed，1 条观察/已排除
- 阅读提示：本报告的“已验证”指实际观察与验收预期一致；“冲突”指实际观察与预期不一致，需人工复核验收或目标状态。

## 概述

- 目标：
- 范围：
- 授权依据：<RoE / 工单 / 授权书 ID>（生产使用必须有）
- 运行：run <N>，<started> – <finished>，<steps> 步，<tokens> tokens
- 工具/模型版本：<tool versions> / <model>
- 终止原因：<completed / plateau / budget / failed>

## 资产与暴露面

> 客户往往先看这一节再看漏洞。全部从线索确定性推导，不编造。

- 开放端口：h:80、h:443
- 已确认关闭：h:21、h:22（等 N 个）
- DNS 解析：1.2.3.4、5.6.7.8
- Web 端点：http://h/ → 200；http://h/robots.txt → 200；（另有 N 条）
- TLS：h:443 TLSv1.3 · TLS_AES_256_GCM_SHA384 · verified=true · 90 天后到期

## 验收核对矩阵

| 验收项 | 预期 | 实际观察 | 判定 | 证据 | 置信度 |
|---|---|---|---|---|---|
| acc-1 80 端口开放 | probe.open | probe.open @ h:80 | VERIFIED | `ev-...` | 高 |
| acc-2 443 端口关闭 | probe.closed | tls.cert @ h:443（TLS 握手成功） | CONTRACT_CONFLICT | `ev-...` | 中（需复核） |
| acc-3 根路径 301 | http.status.301 | http.status.200 @ / | CONTRACT_CONFLICT | `ev-...` | 中（需复核） |
| acc-4 /robots.txt 200 | http.status.200 | http.status.200 @ /robots.txt | VERIFIED | `ev-...` | 高 |
| acc-5 /admin/ 暴露 | http.status.302 | http.status.200 @ /admin/（catch-all） | CONFLICT / 非暴露 | `ev-...` | 中 |
| acc-6 /backup.zip 暴露 | http.error | http.status.200 @ /backup.zip（catch-all） | CONFLICT / 非暴露 | `ev-...` | 中 |

判定枚举：
- `VERIFIED`：实际观察与验收预期一致。
- `CONTRACT_CONFLICT`：观察到同族证据，但值/状态与预期不一致；需人工修订验收或目标状态。
- `NOT_OBSERVED`：没有采到可用证据。
- `NOT_EXECUTED`：任务/动作未执行。

## 已达成验收项（严格验证）

- acc-1：...
- acc-4：...

> 同族观察（例如用 200 满足“预期 301”的家族匹配）不列入本节；见矩阵。

## 发现

### Confirmed

### Suspected

### 已排除 / 观察

- OBS-1（t-0012）：/admin/ 与 /backup.zip 返回 200，但正文与根路径一致，属 SPA catch-all，不构成真实暴露。
- INFO-TLS（t-0005）：h:443 TLSv1.3 · <cipher> · verified=true · days_left=50。
- INFO-PORTS：开放端口 h:80、h:443。

### 信息性观察

- 安全响应头未采集：当前 http 工具不返回 headers（工具能力缺口），不能写“缺失安全头”。

## 未达成项与原因

- acc-2：...
  - 原因：验收预期 probe.closed 与实际 tls.cert @ h:443 冲突（不是没采到，是预期写错/需人工复核）。
  - 未完成任务：...
- acc-3：...
  - 原因：已观察到 http.status.200，但未达到预期 http.status.301。

## 覆盖与限制

- 已验证：80 开放、根路径 200、robots.txt 200、TLS 证书。
- 未验证：安全响应头、/admin/ 与 /backup.zip 的真实内容、DNS A 记录、其他端口/路径。
- 工具限制：<http 不返回 headers / shell 白名单不含 nslookup / ...>
- 执行限制：blocked/pending 任务及原因：...

## 执行质量（内部）

- 结算：<steps> 步 · <tokens> tokens
- 任务：done / failed / blocked / pending
- 派发/重试：worker 返回 N 次，restart N 次
- 质量信号：virtual_success N 次，Model 解析失败 N 次
- 计划模式：planner / fallback:planner_error / fallback:invalid_criteria
- 终态：completed / plateau(thrashing) / budget / failed

## 下一步

1. 复核 acc-2：验收预期 probe.closed 与实际 tls.cert 冲突，确认目标真实状态后修订验收或补采。
2. 补充 acc-3：重新请求根路径，记录实际状态码；不要把 200 当成 301 已验证。
3. 用 http 采集响应头/长度/Content-Type，补安全响应头维度。
4. 把 catch-all 结论写入“已排除发现”，避免重复误报。

## 证据附录

| Evidence | SHA256 前 12 位 | 工具 | 命令/参数 | 来源任务 |
|---|---|---|---|---|
| `ev-...` | ... | http | GET http://h/ | t-0003 |

> 证据全文在 `missions/<family>/runs/<N>/evidence/`；报告只给 id + sha 前缀，客户可独立校验。
```

## 状态判定规则

```text
NOT_EXECUTED       任务图为空 / bootstrap 失败 / 没有任何动作
FAILED             mission 以 failed 结束（规划失败、不可恢复错误）
CONTRACT_CONFLICT  任一验收项的实际观察与预期值冲突
PARTIAL            有未观察项，或有 failed/blocked/pending 任务
COMPLETED          全部验收项 VERIFIED 且无失败/阻塞/待办
```

## 写作纪律

1. 不把“同族线索存在”写成“预期值已验证”。
2. 0 finding 必须同时给出覆盖范围与未验证项；否则读者会误读为“没有风险”。
3. 每个 finding 必须有：证据引用、复现命令、证伪条件、裁决状态、置信度、来源任务。
4. false_positive / catch-all / 软404 结论必须进入“已排除 / 观察”，不能只躺在账本里。
5. 客户版正文不出现 `t-000x` 内部编号；任务编号放证据附录。
6. 报告必须可从账本重算：同一 state 渲染两次逐字节一致。
