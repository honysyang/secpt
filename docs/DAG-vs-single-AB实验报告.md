# S5 编排层对照实验报告：DAG vs 单链

> 目的：红线 2 要求"复杂度必须有对照数据"。用同一道 CTF 题（ROIS SAML SSO·统一身份认证中心）做 **3 次 DAG 编排 + 3 次单链** 交替对照，判断 S5 编排层是资产还是负债。
>
> 实验日期：2026-10-09 · 题目实例端口：IdP=`58595` / SP=`49263` · 目标 flag：`RCTF{e946a54f-75cf-6d73-9163-ba0959c2dafc}`

---

## 1. 实验条件（对照组同条件）

| 项 | DAG 组 | 单链组 | 备注 |
|----|--------|--------|------|
| 题面（objective） | test.md 全文 | test.md 全文 | 同一文件，含附件下载链接 |
| entries | 2 个（IdP / SP） | 2 个（IdP / SP） | 同一组入口 |
| profile | ctf | ctf | — |
| 命令 | `run --task spec.yaml` | `run --task spec.yaml --no-orchestrator` | 唯一差异：是否启用编排层 |
| 执行 | 交替进行（dag1→single1→dag2→single2→dag3→single3） | 同左 | 避免网络波动集中在一组 |
| 是否改 prompt/参数 | 否 | 否 | 对照组同条件 |
| FakeLLM | 否 | 否 | 验真实能力 |

## 2. 逐轮原始记录

| run | mode | 拿到flag | 总步数(assistant) | 墙钟(ts跨度) | findings | 错误调用 | 链级明细 |
|-----|------|---------|------|------|---------|---------|---------|
| ab-dag-1   | dag | ✅ | 189 | 427.2s | 71 | 62 | exploit-0 → 69步**成功**；exploit-1 → 100步**预算耗尽** |
| ab-dag-2   | dag | ✅ | 212 | 510.3s | 88 | 54 | exploit-0 → 92步**成功**；exploit-1 → 100步**预算耗尽** |
| ab-dag-3   | dag | ✅ | 157 | 544.7s | 88 | 39 | **双链成功**：exploit-0 → 97步；exploit-1 → 40步；verified=2 |
| ab-single-1| single | ❌ | 100 | 375.9s | 33 | 28 | 单链 100步**预算耗尽**（已分析到 XSW 原理，未提交） |
| ab-single-2| single | ✅ | 73 | 343.7s | 33 | 23 | 单链 73步成功 |
| ab-single-3| single | ✅ | 27 | 72.6s | 20 | 6 | 单链 27步成功（**全场最快**） |

## 3. 判据表（原样）

| mode | 成功率 | 平均步数(assistant) | 平均墙钟 | 平均错误调用 |
|------|--------|---------|---------|------------|
| dag | 3/3 | 186.0 | 494.0s | 51.7 |
| single | 2/3 | 66.7 | 264.1s | 19.0 |

### 3.1 口径说明

- **"平均步数/墙钟"口径不对等**：DAG 总步数 = recon + exploit×2 + verify **全部链**的 assistant 事件；单链只有 1 条链。DAG 数值高是架构使然（跑了更多链），不代表其单链能力更差。
- **可比口径 = "成功拿到 flag 的那条链"**：
  - dag 成功链步数：69 / 92 / 97 → 平均 ≈ 86
  - single 成功链步数：73 / 27 → 平均 = 50
  - → 单链成功链平均更快。
- **学习效应**：6 轮顺序执行，模型对 SAML 题越来越熟（single-3 仅 27 步）。"快"部分归因于经验积累，而非模式差异。
- **成功率是唯一不随学习效应漂移的核心指标**：dag 3/3 > single 2/3。

## 4. 两组表现的两派观感

- **支持"编排层保留"**：成功率 3/3 vs 2/3；DAG 具备冗余——一条链预算耗尽时另一条仍成功（ab-dag-1/2 均 exploit-0 成功、exploit-1 耗尽兜底），ab-dag-3 甚至双链奏效。
- **支持"降级为可选开关"**：单链全成功时更快更省步数（27 步），无 DAG 的 recon 与并行链开销；唯一失败（single-1）是预算耗尽而非能力缺失——它已推理到 XSW 攻击原理，只是未在限时内完成提交。

## 5. 附加观察（实验过程中的真实系统行为）

- **附件需自取**：objective 仅给 test.md 原文（含附件下载链接），不做源码路径/线索注入。模型需自行 curl 下载并解包附件才能审计源码——所有轮次都做到了这一步。
- **白名单约束**：`for`、`pwd;`、`cd`、`find` 等不在 bash 白名单（`{awk,base64,cat,curl,file,grep,ls,nmap,openssl,python3,sed,whatweb,xxd}`），模型多数转弯可用 python3 内联规避。
- **write_file 路径陷阱**：exploit-1 链在 DAG run2 中曾因 `write_file` 落盘路径与 bash cwd 不一致而找不到脚本（`python3 can't open .../var/jobs/.../flow.py`），模型靠绝对路径与 sed/read 白盒修正。
- **成功路线统一**：grep 附件（`add-admin.js` / `docker-compose.yml`）找到 `admin / RCTF2025` → 登录 IdP → `GET /saml/idp/Flag` 签发签名断言（NameID=`admin@rois.team`）→ SAMLResponse 原样 POST 到 SP `/saml/acs` → `GET /admin` 拿 FLAG。

---

_生成脚本：`scripts/make_spec.py`（spec 生成）、`scripts/ab_summary.py`（判据汇总）。_