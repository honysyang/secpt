"""优先级修复轮验收：P0-1 token 计费 / P0-2 dangling_dep / P1-1 队列排空
立即收口 / P1-2 验收对象绑定 / P2-1 action.finished 带 tool / P2-2 空轮
不写结算。

每条对应用户本轮的一条裁定，测试名与 STATUS.md ✅ 行一一对应。
"""

from __future__ import annotations

import json
import socket
import time

import pytest

from pa.assets.snapshot import AssetSnapshot
from pa.infra.llm import ApiLLM, MockLLM
from pa.models import AcceptanceItem, Budget, Clue, MissionSpec, MissionState
from pa.cli import main as cli_main
from pa.runtime import budget
from pa.store.view import StoreView
from pa.store.writer import StoreWriter

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


@pytest.fixture
def listener() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    yield port
    s.close()


def _freeze():
    return AssetSnapshot.freeze(ROOT / "pa" / "core" / "prompts", [ROOT / "assets" / "skills"])


def _ledger(root):
    return [
        json.loads(line)
        for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]


class UsageLLM(MockLLM):
    """注入真实 usage 的桩（P0-1）：模拟 OpenAI 兼容端点的 usage 计费，
    每次 complete 记 usage=USAGE——charge() 必须优先用它而不是估算。"""

    USAGE = 1000

    def complete(self, **kw):
        out = super().complete(**kw)
        self.calls[-1]["usage"] = self.USAGE
        return out


# ─────────────────────────── P0-1 token 计费 ───────────────────────────


def test_charge_prefers_real_usage_over_estimate(tmp_mission):
    """charge() 优先取 calls 里的真实 usage；没有 usage 才退回估算。"""

    spec = MissionSpec(
        objective="o",
        targets=["127.0.0.1"],
        acceptance=[
            AcceptanceItem(id="a", item="i", evidence="open", target="127.0.0.1")
        ],
        budget=Budget(),
        allowed_scope=["127.0.0.1"],
    )
    state = MissionState(spec=spec, mission_id="m")
    writer = StoreWriter(tmp_mission)
    llm = MockLLM({"p": ["x"]})
    llm.complete(system="s", user="u", schema=None, phase="p")
    llm.calls[0]["usage"] = 500  # 真实 API 返回的用量
    budget.charge(state, writer, llm, [0], time.monotonic())
    assert state.spent_tokens == 500  # 不是 estimate("su") 的个位数
    # 无 usage 的调用退回估算（向后兼容 MockLLM 旧脚本）
    llm.complete(system="s", user="u", schema=None, phase="p")
    llm.calls[-1].pop("usage", None)  # MockLLM 默认不带 usage 键
    budget.charge(state, writer, llm, [1], time.monotonic())
    assert state.spent_tokens == 500 + llm.estimate_tokens("su")


def test_api_llm_records_usage_from_response(monkeypatch):
    """ApiLLM 把响应里的 usage 记入 calls（真实计费链路的来源）。"""

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [{"message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            }

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers, json):
            return FakeResp()

    monkeypatch.setattr("pa.infra.llm.httpx.Client", FakeClient)
    client = ApiLLM(base_url="http://x/v1", api_key="k", model="m")
    assert client.complete(system="s", user="u", schema=None, phase="p") == "{}"
    assert client.calls[0]["usage"] == 18
    assert "api_key" not in repr(client.calls[0])


def test_token_budget_terminates_budget_tokens(tmp_mission, listener):
    """注入 usage 的桩跑真实 run()：spent_tokens > 0，tokens 触顶收口
    为 budget_tokens（预算从此真正生效）。"""
    from pa.runtime import run

    planner = json.dumps(
        {
            "tasks": [
                {
                    "type": "recon",
                    "goal": f"probe 127.0.0.1:{listener}",
                    "success_criteria": [{"evidence": "never-met"}],
                    "scope_slice": {"host": "127.0.0.1"},
                    "allowed_tools": ["probe"],
                    "budget": {"steps": 4},
                }
            ],
            "current_strategy": {},
            "rationale": "r",
        },
        ensure_ascii=False,
    )
    worker = json.dumps(
        {
            "kind": "call_tool",
            "tool": "probe",
            "args": {"target": "127.0.0.1", "port": listener},
            "expect": "open",
        },
        ensure_ascii=False,
    )
    llm = UsageLLM({"planner": [planner], "worker": [worker]})
    spec = MissionSpec(
        objective="探测本地监听端口",
        targets=["127.0.0.1"],
        acceptance=[
            AcceptanceItem(
                id="acc-1",
                item="端口开放被证实",
                evidence="never-met",
                target="127.0.0.1",
            )
        ],
        budget=Budget(steps=12, seconds=600, tokens=1500),
        allowed_scope=[f"127.0.0.1:{listener}"],
    )
    state = run(tmp_mission, spec, llm=llm, assets=_freeze())
    assert state.spent_tokens > 0
    assert state.finish_reason == "budget_tokens"
    charged = [e for e in _ledger(tmp_mission) if e["type"] == "budget.charged"]
    assert sum(e["payload"]["tokens"] for e in charged) == state.spent_tokens


# ─────────────────────────── P0-2 dangling_dep ───────────────────────────


def test_dangling_dep_blocked_with_i3_not_pending(tmp_mission):
    """mock planner 返回 deps=["t-9999"] → 任务 blocked(dangling_dep) +
    mentor.intervened(I3)，绝不停在 pending。"""
    from pa.runtime import run

    tasks = [
        {
            "type": "recon",
            "goal": "probe 127.0.0.1:80",
            "success_criteria": [{"evidence": "probe.closed"}],
            "scope_slice": {},
            "allowed_tools": ["probe"],
            "budget": {"steps": 4},
        },
        {
            "type": "recon",
            "goal": "依赖不存在的后续任务",
            "success_criteria": [{"evidence": "probe.closed"}],
            "deps": ["t-9999"],
            "scope_slice": {},
            "allowed_tools": ["probe"],
            "budget": {"steps": 2},
        },
    ]
    planner = json.dumps(
        {"tasks": tasks, "current_strategy": {}, "rationale": "r"}, ensure_ascii=False
    )
    worker = json.dumps(
        {
            "kind": "call_tool",
            "tool": "probe",
            "args": {"target": "127.0.0.1", "port": 80},
            "expect": "x",
        },
        ensure_ascii=False,
    )
    spec = MissionSpec(
        objective="o",
        targets=["127.0.0.1"],
        acceptance=[
            AcceptanceItem(id="acc-1", item="i", evidence="never", target="127.0.0.1")
        ],
        budget=Budget(steps=12, seconds=600, tokens=40000),
        allowed_scope=["127.0.0.1:80"],
    )
    state = run(
        tmp_mission,
        spec,
        llm=MockLLM({"planner": [planner], "worker": [worker]}),
        assets=_freeze(),
    )
    t2 = state.tasks["t-0002"]
    assert t2.state == "blocked" and t2.block_reason == "dangling_dep"
    assert state.tasks["t-0001"].state == "done"
    intervened = [e for e in _ledger(tmp_mission) if e["type"] == "mentor.intervened"]
    assert any(
        e["payload"].get("issue") == "dangling_dep"
        and e["payload"].get("level") == "I3"
        for e in intervened
    )


def test_resolvable_deps_dropped_tasks_parallel(tmp_mission):
    """合法依赖（指向本批次任务）被丢弃：t-0002 不等 t-0001，全部并列。"""
    from pa.runtime import run

    tasks = [
        {
            "type": "recon",
            "goal": "probe 127.0.0.1:80",
            "success_criteria": [{"evidence": "probe.closed"}],
            "scope_slice": {},
            "allowed_tools": ["probe"],
            "budget": {"steps": 4},
        },
        {
            "type": "recon",
            "goal": "并列后续任务",
            "success_criteria": [{"evidence": "probe.closed"}],
            "deps": ["t-0001"],
            "scope_slice": {},
            "allowed_tools": ["probe"],
            "budget": {"steps": 4},
        },
    ]
    planner = json.dumps(
        {"tasks": tasks, "current_strategy": {}, "rationale": "r"}, ensure_ascii=False
    )
    worker = json.dumps(
        {
            "kind": "call_tool",
            "tool": "probe",
            "args": {"target": "127.0.0.1", "port": 80},
            "expect": "x",
        },
        ensure_ascii=False,
    )
    spec = MissionSpec(
        objective="o",
        targets=["127.0.0.1"],
        acceptance=[
            AcceptanceItem(id="acc-1", item="i", evidence="never", target="127.0.0.1")
        ],
        budget=Budget(steps=12, seconds=600, tokens=40000),
        allowed_scope=["127.0.0.1:80"],
    )
    state = run(
        tmp_mission,
        spec,
        llm=MockLLM({"planner": [planner], "worker": [worker]}),
        assets=_freeze(),
    )
    assert state.tasks["t-0002"].deps == []
    dispatched = {
        e["payload"]["task_id"]
        for e in _ledger(tmp_mission)
        if e["type"] == "task.dispatched"
    }
    assert dispatched == {"t-0001", "t-0002"}  # 第 1 轮全部派发


# ─────────────────────────── P1-1 队列排空立即收口 ───────────────────────────


def test_empty_plan_falls_back_to_baseline(tmp_mission):
    """planner 产出零任务：不再 0 任务 plateau，而是落确定性基线计划。

    这是 runs/2 事故的修复：没有可执行任务的 mission 不能生成“0 发现”
    正常报告；planner 缺席时至少 probe/HTTP/TLS 基线要跑起来。
    """
    from pa.runtime import run

    planner = json.dumps(
        {"tasks": [], "current_strategy": {}, "rationale": "empty"}, ensure_ascii=False
    )
    spec = MissionSpec(
        objective="o",
        targets=["127.0.0.1"],
        acceptance=[
            AcceptanceItem(id="acc-1", item="i", evidence="never", target="127.0.0.1")
        ],
        budget=Budget(steps=12, seconds=600, tokens=40000),
        allowed_scope=["127.0.0.1:80"],
    )
    state = run(
        tmp_mission, spec, llm=MockLLM({"planner": [planner]}), assets=_freeze()
    )
    assert state.tasks, "空计划必须回落确定性基线，不能 0 任务"
    goals = " ".join(t.goal for t in state.tasks.values())
    assert "127.0.0.1:80" in goals
    assert state.finish_reason != "completed"  # acceptance=never，不会成功
    assert state.finish_reason in ("plateau", "budget_steps", "budget_tokens")


# ─────────────────────────── P1-2 验收对象绑定 ───────────────────────────


def test_acceptance_requires_target_binding():
    """两条 probe.open 绑不同 target，只满足其一对 → 验收未达成。"""
    from pa.runtime.loop import _acceptance_met, _item_met

    spec = MissionSpec(
        objective="o",
        targets=["10.0.0.5", "10.0.0.6"],
        acceptance=[
            AcceptanceItem(
                id="a1",
                item="10.0.0.5 的 80 端口开放",
                evidence="probe.open",
                target="10.0.0.5",
            ),
            AcceptanceItem(
                id="a2",
                item="10.0.0.6 的 80 端口开放",
                evidence="probe.open",
                target="10.0.0.6",
            ),
        ],
        budget=Budget(),
        allowed_scope=["10.0.0.5:80", "10.0.0.6:80"],
    )
    state = MissionState(spec=spec, mission_id="m")
    state.clues.append(
        Clue(kind="probe.open", key="10.0.0.5:80", value="open", target="10.0.0.5")
    )
    assert _item_met(state, spec.acceptance[0])
    assert not _item_met(state, spec.acceptance[1])
    assert not _acceptance_met(state)
    # 补全第二个对象 → 达成
    state.clues.append(
        Clue(kind="probe.open", key="10.0.0.6:80", value="open", target="10.0.0.6")
    )
    assert _acceptance_met(state)


def test_acceptance_path_matching():
    """path 参与三要素：声明了路径就必须路径吻合；path=None 不区分路径。"""
    from pa.runtime.loop import _item_met

    spec = MissionSpec(
        objective="o",
        targets=["h"],
        acceptance=[
            AcceptanceItem(
                id="a1",
                item="后台可访问",
                evidence="http.status.200",
                target="h",
                path="/admin",
            ),
            AcceptanceItem(
                id="a2", item="站点可访问", evidence="http.status.200", target="h"
            ),
        ],
        budget=Budget(),
        allowed_scope=["h:80"],
    )
    state = MissionState(spec=spec, mission_id="m")
    state.clues.append(
        Clue(
            kind="http.status.200",
            key="http://h/",
            value="status=200",
            target="h",
            path="/",
        )
    )
    assert not _item_met(state, spec.acceptance[0])  # 路径不吻合
    assert _item_met(state, spec.acceptance[1])  # path=None 不区分


def test_gen_acceptance_drops_objectless_kinds():
    """shell.ok / shell.error / fs.result 无对象证据类型不得作验收；
    保留项必须绑定 target。"""
    from pa.core.nl import gen_acceptance

    raw = json.dumps(
        {
            "items": [
                {"item": "10.0.0.5 的 80 端口可访问", "evidence": "probe.open"},
                {"item": "随便哪台执行成功", "evidence": "shell.ok"},
                {"item": "脚本失败也要算", "evidence": "shell.error"},
                {"item": "文件结果", "evidence": "fs.result"},
            ]
        },
        ensure_ascii=False,
    )
    kept, warnings, intent, dims, sketch = gen_acceptance(
        "扫描 10.0.0.5", ["10.0.0.5"], MockLLM({"do": [raw]})
    )
    assert [a.evidence for a in kept] == ["probe.open"]
    assert kept[0].target == "10.0.0.5:80"  # item 写明端口 → 绑 host:port
    assert sum(1 for w in warnings if "无对象" in w) == 3


# ─────────────────── 意图驱动：理解意图 → 规划（do 立面） ───────────────────


def test_gen_acceptance_intent_and_dimensions():
    """新形态输出：intent / dimensions 进 spec，简报带意图与维度。"""
    from pa.core.nl import build_spec

    raw = json.dumps(
        {
            "intent": "评估 aimohu.com 的对外暴露面，确认有无未授权访问",
            "dimensions": ["端口与服务暴露面", "HTTP 应用识别", "常见敏感路径"],
            "items": [
                {"item": "aimohu.com 的 80 端口开放", "evidence": "probe.open"},
                {"item": "http://aimohu.com/ 返回 2xx", "evidence": "http.status.200"},
            ],
        },
        ensure_ascii=False,
    )
    spec, warnings = build_spec(
        "扫描 aimohu.com，评估安全性", llm=MockLLM({"do": [raw]})
    )
    assert spec.intent == "评估 aimohu.com 的对外暴露面，确认有无未授权访问"
    assert spec.dimensions == ["端口与服务暴露面", "HTTP 应用识别", "常见敏感路径"]
    assert [a.evidence for a in spec.acceptance] == ["probe.open", "http.status.200"]
    assert spec.acceptance[1].target == "aimohu.com"
    # 简报：planner 看得到意图与维度（规划围绕维度展开）
    state = MissionState(spec=spec, mission_id="m")
    from pa.core.context import render_sections

    brief = render_sections("planner", state, {}, _freeze())["brief"]
    assert "intent: 评估 aimohu.com" in brief
    assert "## 评估维度" in brief


def test_gen_acceptance_plan_sketch_into_brief():
    """同一次起草调用产出规划草案：进 spec、预览可用、planner 简报
    可见（## 规划草案）；草案为空时简报不输出该节（逐字节不变）。"""
    from pa.core.nl import build_spec

    raw = json.dumps(
        {
            "intent": "评估暴露面",
            "dimensions": ["端口与服务暴露面"],
            "tasks": [
                {
                    "goal": "探测 80/tcp 与 443/tcp 端口状态",
                    "kind": "probe",
                    "why": "先确认暴露面",
                },
                {"goal": "识别 HTTP 服务与首页响应", "kind": "http"},
            ],
            "items": [{"item": "10.0.0.5 的 80 端口开放", "evidence": "probe.open"}],
        },
        ensure_ascii=False,
    )
    spec, _ = build_spec("扫描 10.0.0.5，评估安全性", llm=MockLLM({"do": [raw]}))
    assert [t.goal for t in spec.plan_sketch] == [
        "探测 80/tcp 与 443/tcp 端口状态",
        "识别 HTTP 服务与首页响应",
    ]
    assert spec.plan_sketch[0].kind == "probe"
    assert spec.plan_sketch[1].kind == "http"
    state = MissionState(spec=spec, mission_id="m")
    from pa.core.context import render_sections

    brief = render_sections("planner", state, {}, _freeze())["brief"]
    assert "## 规划草案" in brief
    assert "探测 80/tcp 与 443/tcp 端口状态（probe）" in brief
    # 草案为空（旧 mission / LLM 未给 tasks）：简报不得出现该节
    empty = MissionState(
        spec=spec.model_copy(update={"plan_sketch": []}), mission_id="m"
    )
    brief2 = render_sections("planner", empty, {}, _freeze())["brief"]
    assert "规划草案" not in brief2


def test_gen_acceptance_rejects_vague_and_duplicate_items():
    """笼统表述（无目标/端口/路径）丢弃并警告；同一 evidence 只留一条。"""
    from pa.core.nl import gen_acceptance

    raw = json.dumps(
        {
            "items": [
                {
                    "item": "对目标域名进行端口探测以确认开放服务",
                    "evidence": "probe.open",
                },
                {
                    "item": "确认目标域名无响应端口或服务关闭状态",
                    "evidence": "probe.closed",
                },
                {"item": "10.0.0.5 的 80 端口开放", "evidence": "probe.open"},
            ]
        },
        ensure_ascii=False,
    )
    kept, warnings, intent, dims, sketch = gen_acceptance(
        "扫描 10.0.0.5", ["10.0.0.5"], MockLLM({"do": [raw]})
    )
    # 笼统的 probe.open 被丢；probe.closed 也是笼统的 → 丢；具体项保留
    assert [a.evidence for a in kept] == ["probe.open"]
    assert kept[0].item == "10.0.0.5 的 80 端口开放"
    assert any("笼统" in w for w in warnings)


def test_gen_acceptance_path_tokens_not_hosts():
    """/robots.txt 这类路径 token 不得被域名正则误当主机而整条误杀；
    http 族证据绑定 path，probe 证据不绑（probe 调用不带路径）。"""
    from pa.core.nl import gen_acceptance

    raw = json.dumps(
        {
            "items": [
                {
                    "item": "请求 http://aimohu.com/robots.txt 检查敏感目录线索",
                    "evidence": "http.error",
                },
                {
                    "item": "探测 aimohu.com 的 80/tcp 是否开放",
                    "evidence": "probe.open",
                },
            ]
        },
        ensure_ascii=False,
    )
    kept, warnings, intent, dims, sketch = gen_acceptance(
        "扫描 aimohu.com", ["aimohu.com"], MockLLM({"do": [raw]})
    )
    assert [a.evidence for a in kept] == ["http.error", "probe.open"]
    assert kept[0].path == "/robots.txt"
    assert kept[1].path is None
    assert not any("范围外" in w for w in warnings)


def test_gen_acceptance_tcp_suffix_is_not_a_path():
    """80/tcp 的斜杠不是路径：acc-3 曾把 path 绑成 /tcp，永远无法命中。"""
    from pa.core.nl import gen_acceptance

    raw = json.dumps(
        {
            "items": [
                {
                    "item": "获取 lake.aimohu.com 基于 80/tcp 的 HTTP 响应状态",
                    "evidence": "http.status.301",
                },
                {
                    "item": "请求 lake.aimohu.com 的 /admin/ 路径并确认响应",
                    "evidence": "http.status.200",
                },
            ]
        },
        ensure_ascii=False,
    )
    kept, warnings, intent, dims, sketch = gen_acceptance(
        "扫描 lake.aimohu.com", ["lake.aimohu.com"], MockLLM({"do": [raw]})
    )
    assert [a.evidence for a in kept] == ["http.status.301", "http.status.200"]
    assert kept[0].target == "lake.aimohu.com:80"
    assert kept[0].path is None, "80/tcp 不能被绑定成 /tcp"
    assert kept[1].path == "/admin/"


def test_gen_acceptance_bare_path_word_not_stray_target():
    """「robots.txt 路径」这种无斜杠路径名引用不得被越界检测当成主机：
    修复前整条误杀（用户 dry-run 实测命中）。"""
    from pa.core.nl import gen_acceptance

    raw = json.dumps(
        {
            "items": [
                {
                    "item": "对目标 lake.aimohu.com 的 robots.txt 路径发起请求，"
                    "确认 HTTP 响应状态",
                    "evidence": "http.status.200",
                },
                {
                    "item": "探测 lake.aimohu.com 的 80 端口是否开放",
                    "evidence": "probe.open",
                },
            ]
        },
        ensure_ascii=False,
    )
    kept, warnings, _, _, _ = gen_acceptance(
        "扫描 lake.aimohu.com", ["lake.aimohu.com"], MockLLM({"do": [raw]})
    )
    assert [a.evidence for a in kept] == ["http.status.200", "probe.open"]
    assert kept[0].target == "lake.aimohu.com"
    assert not any("范围外" in w for w in warnings)


def test_do_preview_shows_intent(tmp_path, monkeypatch, capsys):
    """do 确认表 = 意图/维度/规划（规划草案取代验收项罗列）；
    目录行标注账本位置。"""
    monkeypatch.chdir(tmp_path)
    raw = json.dumps(
        {
            "intent": "评估暴露面",
            "dimensions": ["端口与服务"],
            "tasks": [
                {
                    "goal": "探测 10.0.0.5 的 80/tcp 端口状态",
                    "kind": "probe",
                    "why": "先确认暴露面",
                }
            ],
            "items": [{"item": "10.0.0.5 的 80 端口开放", "evidence": "probe.open"}],
        },
        ensure_ascii=False,
    )
    monkeypatch.setenv("SECPT_LLM_MOCK", json.dumps({"do": [raw]}))
    rc = cli_main(["do", "扫描 10.0.0.5 的 80 端口", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "意图：  评估暴露面" in out
    assert "维度：  端口与服务" in out
    assert "规划：  1. [probe] 探测 10.0.0.5 的 80/tcp 端口状态" in out
    # v0.6 反转旧裁定：验收合同回到确认面板——用户签的是收口合同，
    # 不展示验收等于让用户确认一个自己看不见的契约（报告状态机依赖它）。
    assert "验收：" in out and "acc-1" in out
    assert "账本与日志位置" in out


# ─────────────────────────── P2-1 action.finished 带 tool ───────────────────────────


def test_action_finished_payload_carries_tool(tmp_mission, listener):
    """action.finished payload 必须带 tool：渲染不再退回指纹显示。"""
    from pa.runtime import run

    planner = json.dumps(
        {
            "tasks": [
                {
                    "type": "recon",
                    "goal": f"probe 127.0.0.1:{listener}",
                    "success_criteria": [{"evidence": "open"}],
                    "scope_slice": {},
                    "allowed_tools": ["probe"],
                    "budget": {"steps": 4},
                }
            ],
            "current_strategy": {},
            "rationale": "r",
        },
        ensure_ascii=False,
    )
    worker = json.dumps(
        {
            "kind": "call_tool",
            "tool": "probe",
            "args": {"target": "127.0.0.1", "port": listener},
            "expect": "open",
        },
        ensure_ascii=False,
    )
    spec = MissionSpec(
        objective="探测本地监听端口",
        targets=["127.0.0.1"],
        acceptance=[
            AcceptanceItem(
                id="acc-1", item="端口开放被证实", evidence="open", target="127.0.0.1"
            )
        ],
        budget=Budget(steps=12, seconds=600, tokens=40000),
        allowed_scope=[f"127.0.0.1:{listener}"],
    )
    run(
        tmp_mission,
        spec,
        llm=MockLLM({"planner": [planner], "worker": [worker]}),
        assets=_freeze(),
    )
    finished = [
        e["payload"] for e in _ledger(tmp_mission) if e["type"] == "action.finished"
    ]
    assert finished and all(p.get("tool") == "probe" for p in finished)


# ─────────────────────────── P2-2 空轮不写结算 ───────────────────────────


def test_charge_skips_settle_when_round_inactive(tmp_mission):
    """本轮零 LLM 调用（v0 即零动作）→ 不写 budget.charged，步数不前进。"""
    spec = MissionSpec(
        objective="o",
        targets=["127.0.0.1"],
        acceptance=[
            AcceptanceItem(id="a", item="i", evidence="open", target="127.0.0.1")
        ],
        budget=Budget(),
        allowed_scope=["127.0.0.1"],
    )
    state = MissionState(spec=spec, mission_id="m")
    writer = StoreWriter(tmp_mission)
    n0 = len(list(StoreView(tmp_mission).events(from_seq=1)))
    budget.charge(state, writer, MockLLM({}), [0], time.monotonic())
    n1 = len(list(StoreView(tmp_mission).events(from_seq=1)))
    assert n1 == n0  # 零新事件
    assert state.step == 0  # 步数不前进
    assert state.spent_tokens == 0


# ─────────────── 智能表现修复（真实联调观察 → 裁定） ───────────────


def test_criteria_family_match_and_task_scope():
    """判据语义：同族互为结论（probe.closed 判据被 probe.open 线索满足）；
    task_id 作用域——别的任务的线索不能交差。"""
    from pa.core.reflect import criteria_met
    from pa.models import Criterion

    crit = [Criterion(evidence="probe.closed")]
    own = [
        Clue(
            kind="probe.open",
            key="h:443",
            value="open",
            target="h",
            source_task="t-0002",
        )
    ]
    assert criteria_met(own, crit, task_id="t-0002"), "同族 = 已确认"
    other = [
        Clue(
            kind="probe.open",
            key="h:80",
            value="open",
            target="h",
            source_task="t-0001",
        )
    ]
    assert not criteria_met(other, crit, task_id="t-0002"), "跨任务不交差"
    # 无 task_id（直接调用）保持全局可见
    assert criteria_met(other, crit)


def test_probe_port_level_acceptance():
    """probe 验收写明端口：443 的开放不能替 80 交差。"""
    from pa.runtime.loop import _acceptance_met

    spec = MissionSpec(
        objective="o",
        targets=["h"],
        acceptance=[
            AcceptanceItem(
                id="a1", item="h 的 80/tcp 开放", evidence="probe.open", target="h:80"
            )
        ],
        budget=Budget(),
        allowed_scope=["h:80"],
    )
    state = MissionState(spec=spec, mission_id="m")
    state.clues.append(
        Clue(
            kind="probe.open", key="h:443", value="open", target="h", source_task="t-1"
        )
    )
    assert not _acceptance_met(state)
    state.clues.append(
        Clue(kind="probe.open", key="h:80", value="open", target="h", source_task="t-2")
    )
    assert _acceptance_met(state)


def test_wrong_guess_criterion_task_still_succeeds(tmp_mission, listener):
    """端到端：判据猜错极性（probe.closed）而端口实际开放——任务必须
    成功（真实联调 t-0002 的病：猜 443 关闭 → 永久 virtual_success）。"""
    from pa.runtime import run

    planner = json.dumps(
        {
            "tasks": [
                {
                    "type": "recon",
                    "goal": f"probe 127.0.0.1:{listener}",
                    "success_criteria": [{"evidence": "probe.closed"}],
                    "scope_slice": {},
                    "allowed_tools": ["probe"],
                    "budget": {"steps": 4},
                }
            ],
            "current_strategy": {},
            "rationale": "r",
        },
        ensure_ascii=False,
    )
    worker = json.dumps(
        {
            "kind": "call_tool",
            "tool": "probe",
            "args": {"target": "127.0.0.1", "port": listener},
            "expect": "closed",
        },
        ensure_ascii=False,
    )
    spec = MissionSpec(
        objective="探测端口状态",
        targets=["127.0.0.1"],
        acceptance=[
            AcceptanceItem(
                id="acc-1",
                item="端口状态已确认",
                evidence="probe.open",
                target="127.0.0.1",
            )
        ],
        budget=Budget(steps=12, seconds=600, tokens=40000),
        allowed_scope=[f"127.0.0.1:{listener}"],
    )
    state = run(
        tmp_mission,
        spec,
        llm=MockLLM({"planner": [planner], "worker": [worker]}),
        assets=_freeze(),
    )
    assert state.tasks["t-0001"].state == "done"
    assert state.finish_reason == "completed"


def test_worker_novelty_stops_catch_all(tmp_mission):
    """catch-all 站点：每条新路径都是新 key 但 (kind,value) 重复——
    worker 必须按信息增益早停，不能烧光任务预算（真实联调 t-0006 的病：
    11 次决策全返回 200）。"""
    from pa.core.worker import run_worker
    from pa.infra.executor import ProcResult
    from pa.models import Criterion
    from pa.tools import REGISTRY, ToolSpec

    original = REGISTRY["http"]

    def fn(args, ctx):
        return ProcResult(200, b"<html>same body</html>", b"")

    REGISTRY["http"] = ToolSpec(
        name="http",
        risk_tier="low",
        side_effect="read",
        requires=[],
        schema={},
        produces_evidence=True,
        fn=fn,
    )
    try:
        state, writer, assets, task = _worker_setup(tmp_mission)
        task.allowed_tools = ["http"]
        # 判据与本族产出无关（probe）：http 结果不可能满足它——专门
        # 走信息增益路径；若按旧 key 新颖度规则，每条新路径都算
        # "新线索"，worker 会烧光 12 步预算
        task.success_criteria = [Criterion(evidence="probe.open")]
        script = [
            json.dumps(
                {
                    "kind": "call_tool",
                    "tool": "http",
                    "args": {"url": f"http://h/{p}"},
                    "expect": "触发错误",
                },
                ensure_ascii=False,
            )
            for p in ("a", "b", "c", "d", "e", "f")
        ]
        res = run_worker(task, state, writer, MockLLM({"worker": script}), assets)
        assert res.status == "needs_help"
        assert res.cost["steps"] <= 4, "同值线索不再算进展，必须早停"
    finally:
        REGISTRY["http"] = original


def _worker_setup(tmp_mission):
    from pa.models import Criterion, Task

    spec = MissionSpec(
        objective="o",
        targets=["h"],
        acceptance=[AcceptanceItem(id="a1", item="i", evidence="never", target="h")],
        budget=Budget(),
        allowed_scope=["h:80"],
    )
    (tmp_mission / "mission.json").write_text(spec.model_dump_json(), encoding="utf-8")
    state = StoreView(tmp_mission).restore()
    writer = StoreWriter(tmp_mission)
    assets = _freeze()
    task = Task(
        id="t-0001",
        mission_id=tmp_mission.name,
        type="recon",
        goal="探",
        success_criteria=[Criterion(evidence="never")],
        scope_slice={},
        allowed_tools=["http"],
        budget=Budget(steps=12),
    )
    return state, writer, assets, task


def test_sensitive_finding_rules():
    """敏感路径规则：2xx/3xx 命中模式 → finding；4xx 或普通路径 → None。"""
    from pa.core.sensitive import sensitive_finding

    f = sensitive_finding("https://h/.git/config", 200, "ev-1")
    assert f is not None and f.severity == "high"
    assert f.evidence_refs == ["ev-1"] and f.falsify_if
    assert sensitive_finding("https://h/.svn/entries", 301, None) is not None
    assert sensitive_finding("https://h/.env", 200, None) is not None
    # 反向：错误状态 / 普通路径 / 后台路径无发现
    assert sensitive_finding("https://h/.git/config", 404, None) is None
    assert sensitive_finding("https://h/index.html", 200, None) is None
    assert sensitive_finding("https://h/admin/", 200, None) is None


def test_sensitive_finding_lands_and_adjudicates(tmp_mission):
    """端到端：worker 请求 .git/config 得 200 → finding.proposed 落账，
    调度器 JEV sweep 裁决为 suspected（单引用证据不足，不是 confirmed）。"""
    from pa.infra.executor import ProcResult
    from pa.runtime import run
    from pa.tools import REGISTRY, ToolSpec

    original = REGISTRY["http"]

    def fn(args, ctx):
        return ProcResult(200, b"[core]\nrepositoryformatversion = 0\n", b"")

    REGISTRY["http"] = ToolSpec(
        name="http",
        risk_tier="low",
        side_effect="read",
        requires=[],
        schema={},
        produces_evidence=True,
        fn=fn,
    )
    try:
        planner = json.dumps(
            {
                "tasks": [
                    {
                        "type": "recon",
                        "goal": "检查 .git/config 是否可公开读取",
                        "success_criteria": [{"evidence": "http.status.200"}],
                        "scope_slice": {},
                        "allowed_tools": ["http"],
                        "budget": {"steps": 4},
                    }
                ],
                "current_strategy": {},
                "rationale": "r",
            },
            ensure_ascii=False,
        )
        worker = json.dumps(
            {
                "kind": "call_tool",
                "tool": "http",
                "args": {"url": "https://aimohu.test/.git/config"},
                "expect": "200",
            },
            ensure_ascii=False,
        )
        spec = MissionSpec(
            objective="检查敏感路径",
            targets=["aimohu.test"],
            acceptance=[
                AcceptanceItem(
                    id="acc-1",
                    item="版本库配置可访问",
                    evidence="http.status.200",
                    target="aimohu.test",
                    path="/.git/config",
                )
            ],
            budget=Budget(steps=12, seconds=600, tokens=40000),
            allowed_scope=["aimohu.test:443"],
        )
        state = run(
            tmp_mission,
            spec,
            llm=MockLLM({"planner": [planner], "worker": [worker]}),
            assets=_freeze(),
        )
        assert len(state.findings) == 1
        assert ".git/config" in state.findings[0].claim
        kinds = [e["type"] for e in _ledger(tmp_mission)]
        assert "finding.proposed" in kinds and "finding.adjudicated" in kinds
        adj = [e for e in _ledger(tmp_mission) if e["type"] == "finding.adjudicated"]
        assert adj[0]["payload"]["verdict"] == "suspected"
    finally:
        REGISTRY["http"] = original


def test_budget_calibrated_to_real_usage():
    """预算校准：15 分钟任务 18 万 token（实测一轮 5 万+，旧值连 3 轮
    都撑不到）；步数口径不变。"""
    from pa.core.nl import budget_for

    b = budget_for(15, "lab")
    assert b.tokens == 180000
    assert b.seconds == 900 and b.steps == 30
    assert budget_for(1, "lab").tokens == 12000


def test_dotenv_load_fills_without_overriding(tmp_path, monkeypatch):
    """仓库根 .env 自动载入的解析语义：填补未设置的键，真实环境优先；
    注释/空行/引号处理正确。"""
    import os
    from pa import envs

    f = tmp_path / ".env"
    f.write_text(
        '# 注释行\n\nSECPT_TEST_DOTENV_A=one\nSECPT_TEST_DOTENV_B="two"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("SECPT_TEST_DOTENV_A", raising=False)
    monkeypatch.delenv("SECPT_TEST_DOTENV_B", raising=False)
    monkeypatch.setenv("SECPT_TEST_DOTENV_B", "env-wins")
    assert envs._load_dotenv(f) == 1
    assert os.environ["SECPT_TEST_DOTENV_A"] == "one"
    assert os.environ["SECPT_TEST_DOTENV_B"] == "env-wins"  # 不覆盖
    assert envs._load_dotenv(tmp_path / "missing.env") == 0


# ───────────── 可读日志 secpt log + report 验收判定口径统一 ─────────────


def test_report_acceptance_matches_loop_verdict(tmp_path):
    """回归：report 的验收达成判定与主循环同一出口——443 探得 open
    时「probe.closed @host:443」的验收也必须算达成（修复前 report 用
    旧的宽松比对，同一账本出现「收口 3/6、报告 0/6」矛盾）。"""
    from pa.report import render_report

    spec = MissionSpec(
        objective="扫描 lake.aimohu.com",
        targets=["lake.aimohu.com"],
        acceptance=[
            AcceptanceItem(
                id="acc-1",
                item="443 端口状态已确认",
                evidence="probe.closed",
                target="lake.aimohu.com:443",
            )
        ],
        budget=Budget(steps=4, seconds=60, tokens=1000),
        allowed_scope=["lake.aimohu.com"],
    )
    state = MissionState(spec=spec, mission_id="m")
    state.clues.append(
        Clue(
            kind="probe.open",
            key="lake.aimohu.com:443",
            value="open",
            target="lake.aimohu.com",
            source_task="t-1",
        )
    )
    state.finish_reason = "plateau"
    text = render_report(state, StoreView(tmp_path))
    assert "1/1 项达成" in text


def _run_probe_mission(root, listener):
    """最小可跑 mission：planner 一个 probe 任务 + worker 一次探开。"""
    from pa.runtime import run

    planner = json.dumps(
        {
            "tasks": [
                {
                    "type": "recon",
                    "goal": f"probe 127.0.0.1:{listener}",
                    "success_criteria": [{"evidence": "open"}],
                    "scope_slice": {},
                    "allowed_tools": ["probe"],
                    "budget": {"steps": 4},
                }
            ],
            "current_strategy": {},
            "rationale": "r",
        },
        ensure_ascii=False,
    )
    worker = json.dumps(
        {
            "kind": "call_tool",
            "tool": "probe",
            "args": {"target": "127.0.0.1", "port": listener},
            "expect": "open",
            "rationale": "先确认端口状态",
        },
        ensure_ascii=False,
    )
    spec = MissionSpec(
        objective="探测本地监听端口",
        targets=["127.0.0.1"],
        acceptance=[
            AcceptanceItem(
                id="acc-1", item="端口开放被证实", evidence="open", target="127.0.0.1"
            )
        ],
        budget=Budget(steps=12, seconds=600, tokens=40000),
        allowed_scope=[f"127.0.0.1:{listener}"],
    )
    run(
        root,
        spec,
        llm=MockLLM({"planner": [planner], "worker": [worker]}),
        assets=_freeze(),
    )


def test_action_started_carries_expect_rationale(tmp_mission, listener):
    """action.started payload 带 expect/rationale：思考链在账本可见。"""
    _run_probe_mission(tmp_mission, listener)
    started = [
        e["payload"] for e in _ledger(tmp_mission) if e["type"] == "action.started"
    ]
    assert started and all(p.get("expect") == "open" for p in started)
    assert all(p.get("rationale") == "先确认端口状态" for p in started)


def test_log_readable_and_readonly(tmp_mission, listener):
    """secpt log：叙事四段齐全（任务/计划/执行/收口）、决策可见、
    确定性（两次渲染逐字节相等）、只读（账本 sha256 不变）。"""
    import hashlib

    _run_probe_mission(tmp_mission, listener)
    ledger = tmp_mission / "events.jsonl"
    before = hashlib.sha256(ledger.read_bytes()).hexdigest()
    from pa.report.log import render_log

    log = render_log(tmp_mission)
    assert "## 编排决策" in log, "每轮编排者的 run/create 理由应可读"
    assert "## 执行过程" in log
    assert "决策：调用 probe" in log
    assert f'"port": {listener}' in log
    assert "预期：open" in log
    assert "理由：先确认端口状态" in log
    assert "- 思考：判据已满足" in log, "worker 提前收工的理由应可见"
    assert "判定：done" in log
    assert "## 线索" in log and "probe.open" in log
    assert "## 收口" in log and "验收：1/1 达成" in log
    assert render_log(tmp_mission) == log, "确定性"
    assert hashlib.sha256(ledger.read_bytes()).hexdigest() == before, "只读"


# ───────────── tls 工具 + 覆盖度判据 + 收口跑空队列 ─────────────


def test_tls_clue_extraction_value_fields():
    """tls 成功 → tls.cert 一条，value 为证书字段 dict（含 days_left/
    verified）；失败 → tls.error 一条。key 均为 host:port。"""
    from types import SimpleNamespace
    from pa.core.reflect import extract_clues

    out = SimpleNamespace(
        exit_code=0,
        stderr=b"",
        stdout=b"handshake=TLSv1.3\nverified=true\ndays_left=50\n",
        evidence_id="ev-t",
    )
    c = extract_clues("tls", {"target": "lake.aimohu.com", "port": 443}, out)[0]
    assert c.kind == "tls.cert" and c.key == "lake.aimohu.com:443"
    assert c.target == "lake.aimohu.com"
    assert c.value["days_left"] == "50" and c.value["verified"] == "true"
    out.exit_code = 1
    out.stderr = b"SSLError: handshake failure"
    c = extract_clues("tls", {"target": "lake.aimohu.com", "port": 443}, out)[0]
    assert c.kind == "tls.error" and "handshake failure" in c.value


def test_coverage_criteria_counts_distinct_keys():
    """覆盖度判据「族:条数」：按 clue.key 去重计数，防最小功交差——
    枚举任务只探一个端点喂不饱 http:3；同 URL 重复请求只算一条；
    别的任务的线索不算数（任务作用域）。"""
    from pa.core.reflect import criteria_met
    from pa.models import Clue, Criterion

    def http_clue(key, task):
        return Clue(
            kind="http.status.200",
            key=key,
            value="status=200",
            target="lake.aimohu.com",
            source_task=task,
        )

    crit = [Criterion(evidence="http:3")]
    assert not criteria_met([http_clue("u1", "t-1")], crit, task_id="t-1"), "1 条喂不饱"
    assert not criteria_met(
        [http_clue("u1", "t-1"), http_clue("u1", "t-1")], crit, task_id="t-1"
    ), "同 key 去重"
    assert not criteria_met(
        [http_clue("u1", "t-1"), http_clue("u2", "t-1"), http_clue("u3", "t-2")],
        crit,
        task_id="t-1",
    ), "任务作用域"
    assert criteria_met(
        [http_clue("u1", "t-1"), http_clue("u2", "t-1"), http_clue("u3", None)],
        crit,
        task_id="t-1",
    ), "未归属线索可交差"
    # 覆盖度与普通判据 AND：普通判据不满足时整体不满足
    crit2 = [Criterion(evidence="http:2"), Criterion(evidence="probe.open")]
    assert not criteria_met(
        [http_clue("u1", "t-1"), http_clue("u2", "t-1")], crit2, task_id="t-1"
    )
    crit3 = [Criterion(evidence="tls:1")]
    assert criteria_met(
        [
            Clue(
                kind="tls.cert",
                key="lake.aimohu.com:443",
                value={},
                target="lake.aimohu.com",
                source_task="t-1",
            )
        ],
        crit3,
        task_id="t-1",
    )


def test_completed_waits_for_queue_drain(tmp_mission, listener):
    """收口口径：验收达成但队列未排空不收工——两个任务都跑完才
    completed（旧口径会在验收达成时立即收口，留下从未派发的任务）。"""
    from pa.runtime import run

    tasks = [
        {
            "type": "recon",
            "goal": f"probe A 127.0.0.1:{listener}",
            "success_criteria": [{"evidence": "open"}],
            "scope_slice": {},
            "allowed_tools": ["probe"],
            "budget": {"steps": 4},
        },
        {
            "type": "recon",
            "goal": f"probe B 127.0.0.1:{listener}",
            "success_criteria": [{"evidence": "open"}],
            "scope_slice": {},
            "allowed_tools": ["probe"],
            "budget": {"steps": 4},
        },
    ]
    planner = json.dumps(
        {"tasks": tasks, "current_strategy": {}, "rationale": "r"}, ensure_ascii=False
    )
    worker = json.dumps(
        {
            "kind": "call_tool",
            "tool": "probe",
            "args": {"target": "127.0.0.1", "port": listener},
            "expect": "open",
        },
        ensure_ascii=False,
    )
    spec = MissionSpec(
        objective="探测本地监听端口",
        targets=["127.0.0.1"],
        acceptance=[
            AcceptanceItem(
                id="acc-1", item="端口开放被证实", evidence="open", target="127.0.0.1"
            )
        ],
        budget=Budget(steps=12, seconds=600, tokens=40000),
        allowed_scope=[f"127.0.0.1:{listener}"],
    )
    state = run(
        tmp_mission,
        spec,
        llm=MockLLM({"planner": [planner], "worker": [worker]}),
        assets=_freeze(),
    )
    assert state.finish_reason == "completed"
    assert all(t.state == "done" for t in state.tasks.values()), (
        "验收达成后未跑完的任务必须继续跑完"
    )
