# secpt

无人值守的安全测试智能体骨架。设计见 `../CONTROL_PLANE.md`，架构见 `../ARCHITECTURE.md`，施工单见 `../BUILD_SPEC.md`。

> 仅面向**获得书面授权**的渗透测试、红队演练与靶场评测。

## 安装

```bash
pip install -e .
```

## 快速开始（不需要 API key）

```bash
python tests/run_all.py          # 无 pytest 依赖的自测
pytest -q                        # 或标准方式
```

## 真实运行

```bash
cp .env.example .env             # 填 SECPT_API_KEY / SECPT_BASE_URL / SECPT_MODEL
secpt run --targets targets.txt --acceptance acceptance.yaml \
          --budget 30m --out missions/m1
secpt status missions/m1
secpt report missions/m1
secpt resume missions/m1
```

`targets.txt` 一行一个目标：`10.0.0.5:445`、`app.example.com:443`。

`acceptance.yaml`：

```yaml
- id: a1
  item: 完成存活主机与端口清单
  evidence: probe.open
  judged_by: auto
- id: a2
  item: 验证至少一个可利用漏洞并给出 POC
  evidence: exploitable
  judged_by: jev
```

不传 `--acceptance` 时生成一条默认验收项。

未安装时可直接用模块方式：`python -m pa.cli run --out missions/m1 ...`。

## 目录

```
pa/          Python 包（短名，可重命名为 secpt/）
  engine/    纯状态机（禁止 import 模型客户端）
  core/      无状态决策层
  mentor/    教练：计划评审 + 漂移检测
  jev/       结果裁决
  infra/     证据库 / 执行器 / 模型客户端
  tools/     工具实现（shell / http / fs / probe）
skills/      技能库（markdown）
missions/    运行时数据（事件账本 + 证据 + 报告）
```

> 包名 `pa` 是短名，与项目名 `secpt` 不冲突；若要改成 `secpt/`，见 `ARCHITECTURE.md` 附录的重命名命令。

## 关键约定

- `events.jsonl` 是唯一真源；`snapshot.json` 只是加速恢复。
- 所有副作用经 `pa/engine/gateway.py`，越界动作不会到达执行器。
- 证据内容寻址、不可变；Finding 必须挂 `evidence_refs`。
- 引擎目录里不出现任何 provider 名称：

```bash
grep -rE "openai|anthropic|httpx" pa/engine/ || echo "layering ok"
```

## v0 已知边界

- 同步执行，无并发（M3 引入进程池）。
- Mentor 只有代码检查，模型评审留到 v1。
- JEV 是纯规则裁决，`suspected` 的模型复核留到 v1。
- 不支持破坏性工具；`shell` 有命令白名单。
