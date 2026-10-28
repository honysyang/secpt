"""M3-3 探针 observations 全链（先测试）：配置开关/伪事件源/注入窗/e2e/R0 扩判。

蓝本锚点：observations DDL §5 原样入 0004 迁移；锚点顺序 findings.evidence_ref
优先锚 observations（§3.2）；R6 独立开关 enable_http_probe 默认关，关=零开销。
四项拍板（§69）：①开关落 profile.toml [probes] 段；②注入窗=registry _dispatch
执行窗全包（denied/recursion 零抓取）；③后端=注入式 CaptureBackend，默认 mock
（ScriptedCapture，meta 诚实标 mock:true，snapshot 落真文件），mitmproxy fail-fast
M4 接线；④锚点 obs:<id> 双锚 + R0 扩判（行存在且 meta/snapshot 非空）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from secpt.core.loop import PentestLoop
from secpt.errors import LedgerError, SecptError
from secpt.finalize.exit_checklist import ExitChecklist
from secpt.judge.profile import GroundTruth, JudgeProfile
from secpt.judge.r0_self import r0
from secpt.judge.sweep import sweep
from secpt.llms.client import LlmClient
from secpt.llms.fake import FakeLLM
from secpt.llms.main_llm import MainLlm
from secpt.store import ledger, observations
from secpt.store import migrate as migrate_store
from secpt.tools.adapters import AgentRunTool
from secpt.tools.guard import GuardConfig
from secpt.tools.probe import (
    ProbeConfig,
    ScriptedCapture,
    build_backend,
    load_probe_config,
    note_for,
    wrap_execution,
)
from secpt.tools.registry import ToolManifest, ToolOutput, ToolRegistry

# ---------- 测试用替身 ----------------------------------------------------------------


class _EchoTool:
    """记账适配器：Guard/探针测试共用——拦下则 calls 恒 0。"""

    manifest = ToolManifest(name="danger", kind="mcp", description="测试工具", params={})

    def __init__(self) -> None:
        self.calls = 0

    def run(self, args: Mapping[str, str]) -> ToolOutput:
        self.calls += 1
        return ToolOutput(
            tool_name=self.manifest.name,
            args=dict(args),
            exit_code=0,
            summary="执行过一次",
            raw_ref="",
        )


class _CountingCapture:
    """计数型抓包替身：拦截点应发生在 start 之前，starts 恒 0 可证。"""

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> list:
        self.stops += 1
        return []


class _FailStartCapture:
    """起抓/收抓都会炸的替身：可降级器官——炸了也不许挡执行、不许上抛吞回执。"""

    def start(self) -> None:
        raise RuntimeError("抓包起不来")

    def stop(self) -> list:
        raise RuntimeError("抓包收不动")


class _FailStopCapture:
    """执行正常但收抓炸的替身：告警降级，事件空、回执完好。"""

    def start(self) -> None:
        return None

    def stop(self) -> list:
        raise RuntimeError("收抓炸了")


# ---------- 配置开关（拍板①：profile.toml [probes] 段） ------------------------------


class TestProbeConfig:
    """三档语义仿 judge.profile：缺文件降关闭，坏值 E_PROFILE 拒跑（R6 默认关）。"""

    def test_missing_file_or_section_is_disabled(self, tmp_path: Path) -> None:
        assert load_probe_config(str(tmp_path / "ghost.toml")) == ProbeConfig()
        path = tmp_path / "profile.toml"
        path.write_text("[judge]\nenabled = true\n", encoding="utf-8")
        cfg = load_probe_config(str(path))
        assert cfg.enabled is False
        assert cfg.source is None  # 关闭档 source 不署名，web 面板不会被误导

    def test_enable_flag_configured(self, tmp_path: Path) -> None:
        path = tmp_path / "profile.toml"
        path.write_text("[probes]\nenable_http_probe = true\n", encoding="utf-8")
        cfg = load_probe_config(str(path))
        assert cfg.enabled is True
        assert cfg.source == str(path)

    def test_bad_toml_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "profile.toml"
        path.write_text("[probes\n= not valid ==", encoding="utf-8")
        with pytest.raises(SecptError) as excinfo:
            load_probe_config(str(path))
        assert excinfo.value.code == "E_PROFILE"

    def test_bad_value_raises(self, tmp_path: Path) -> None:
        """enable_http_probe 写成字符串 = 操作方笔误，静默降级会掩盖真问题。"""
        path = tmp_path / "profile.toml"
        path.write_text('[probes]\nenable_http_probe = "yes"\n', encoding="utf-8")
        with pytest.raises(SecptError) as excinfo:
            load_probe_config(str(path))
        assert excinfo.value.code == "E_PROFILE"


# ---------- 后端（拍板③：注入式 CaptureBackend + 图纸位） ----------------------------


def test_build_backend_mock_and_failfast(tmp_path: Path) -> None:
    """mock 档 = ScriptedCapture；mitmproxy 档 M4 才接线，装配期 fail-fast 不装假。"""
    assert isinstance(build_backend("mock", tmp_path), ScriptedCapture)
    with pytest.raises(SecptError) as excinfo:
        build_backend("mitmproxy", tmp_path)
    assert excinfo.value.code == "E_PROBE"
    assert "M4" in str(excinfo.value)
    with pytest.raises(SecptError):
        build_backend("tcpdump-ghost", tmp_path)


def test_scripted_capture_emits_mock_events(tmp_path: Path) -> None:
    """伪事件源诚实三件：meta 标 mock:true、snapshot 是刚落盘的真文件、channel=http。"""
    backend = ScriptedCapture(raw_dir=tmp_path)
    backend.start()
    events = backend.stop()
    assert len(events) == 1
    event = events[0]
    assert event.channel == "http"
    assert event.meta["mock"] is True
    snapshot = Path(event.snapshot_ref)
    assert snapshot.is_file() and snapshot.stat().st_size > 0
    assert snapshot.parent == tmp_path


def test_wrap_execution_degrades_on_capture_failure(tmp_path: Path) -> None:
    """抓包炸了 = 可降级器官：告警降级，执行零干扰、回执完好、事件空。"""
    outcome = ToolOutput(tool_name="danger", args={}, exit_code=0, summary="执行过一次", raw_ref="")
    output, events = wrap_execution(_FailStartCapture(), lambda: outcome)
    assert output.summary == "执行过一次"
    assert events == []

    output2, events2 = wrap_execution(_FailStopCapture(), lambda: outcome)
    assert output2.summary == "执行过一次"
    assert events2 == []


# ---------- registry 注入窗（拍板②：guard 判定后、执行窗全包） -----------------------


def test_registry_without_probe_unchanged() -> None:
    """probe=None（关档默认）：行为与 M3-2 逐字节一致，probe_events 恒空——零开销。"""
    plain = ToolRegistry([_EchoTool()]).dispatch("danger", {})
    probed = ToolRegistry([_EchoTool()]).dispatch("danger", {})
    assert plain.model_dump() == probed.model_dump()
    assert probed.probe_events == ()


def test_registry_probe_attaches_events(tmp_path: Path) -> None:
    """开档：执行窗全包成功路径——回执挂 probe_events，snapshot 真文件已落盘。"""
    tool = _EchoTool()
    registry = ToolRegistry([tool], probe=ScriptedCapture(raw_dir=tmp_path))
    receipt = registry.dispatch("danger", {})
    assert tool.calls == 1
    assert len(receipt.probe_events) == 1
    assert Path(receipt.probe_events[0].snapshot_ref).is_file()


def test_registry_denied_zero_capture(tmp_path: Path) -> None:
    """denied 即没执行（拍板②）：窗口根本不开，starts 恒 0——没动作没流量。"""
    tool = _EchoTool()
    capture = _CountingCapture()
    registry = ToolRegistry(
        [tool],
        guard=GuardConfig(tiers={"danger": "high"}, hitl_mode="off"),
        probe=capture,
    )
    receipt = registry.dispatch("danger", {})
    assert tool.calls == 0 and receipt.denied is True
    assert capture.starts == 0


def test_registry_in_process_window(tmp_path: Path) -> None:
    """in-process 工具同样过窗（agent_run 双重身份也取证，蓝本 §3.6 同权口径）。"""
    registry = ToolRegistry([AgentRunTool()], probe=ScriptedCapture(raw_dir=tmp_path))
    receipt = registry.dispatch(
        "agent_run", {"mission": "侦察", "persona": "recon", "step_budget": "3"}
    )
    assert len(receipt.probe_events) == 1
    assert receipt.exit_code is None  # M1 占位回执不理会，但窗口确实开过


# ---------- e2e：observations 落客观流 / 关档零行 ------------------------------------


def _fake_scripts() -> FakeLLM:
    """四条手式（同 test_guard）：agent_run 派单一步 → 收工 → 摘要。"""
    return FakeLLM(
        [
            json.dumps(
                {
                    "target": "10.0.0.5",
                    "objective": "探针全链演示",
                    "success_criteria": "observations 落客观流",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "thought": "派单测试",
                    "plan_updates": [],
                    "tool_calls": [
                        {
                            "name": "agent_run",
                            "args": {
                                "mission": "试一试",
                                "persona": "recon",
                                "step_budget": "3",
                            },
                        }
                    ],
                    "done": False,
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {"thought": "收工", "plan_updates": [], "tool_calls": [], "done": True},
                ensure_ascii=False,
            ),
            json.dumps({"summary": "probe e2e 收束"}, ensure_ascii=False),
        ]
    )


def test_e2e_probe_on_observations_landed(tmp_path: Path) -> None:
    """完成定义③前半：开档时 observations 落客观事件流，meta 诚实标 mock、snapshot 真文件。"""
    conn = migrate_store.connect(":memory:")
    registry = ToolRegistry([AgentRunTool()], probe=ScriptedCapture(raw_dir=tmp_path))
    loop = PentestLoop(
        conn, MainLlm(LlmClient(_fake_scripts())), registry, exit_checker=ExitChecklist()
    )
    result = loop.run("探针演示 10.0.0.5")
    assert result.finished_naturally is True

    rows = observations.task_observations(conn, result.task_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.channel == "http"
    meta: dict[str, Any] = json.loads(row.meta_json)
    assert meta["mock"] is True
    snapshot = Path(row.snapshot_ref)
    assert snapshot.is_file() and snapshot.stat().st_size > 0
    assert row.ts > 0


def test_e2e_probe_off_zero_observations(tmp_path: Path) -> None:
    """完成定义③边界：关档默认零开销——observations 零行、raw 目录零文件。"""
    conn = migrate_store.connect(":memory:")
    registry = ToolRegistry([AgentRunTool()])
    loop = PentestLoop(
        conn, MainLlm(LlmClient(_fake_scripts())), registry, exit_checker=ExitChecklist()
    )
    result = loop.run("探针关闭演示 10.0.0.5")
    assert result.finished_naturally is True
    assert observations.task_observations(conn, result.task_id) == []
    assert not (tmp_path / "raw").exists() or not any((tmp_path / "raw").iterdir())


# ---------- R0 扩判（拍板④：obs:<id> 双锚，文件锚原判据不回退） ----------------------


def test_r0_obs_anchor_accepts_existing_row() -> None:
    """行存在 + meta/snapshot 非空 → obs 锚放行 R0。"""
    conn = migrate_store.connect(":memory:")
    task = ledger.start_task(conn, "锚点演示", "靶机")
    run = ledger.start_run(conn, task.id)
    obs = observations.append(conn, task.id, run.id, "http", {"mock": True}, "raw/http_1.txt")
    finding = ledger.append_finding(conn, task.id, "注入命中", "describe", f"obs:{obs.id}")
    assert r0(conn, finding) is True


def test_r0_obs_anchor_rejects_missing_or_empty() -> None:
    """行不存在 / snapshot 空指针 / 光 obs: 前缀 → 一律判零（不认口头锚）。"""
    conn = migrate_store.connect(":memory:")
    task = ledger.start_task(conn, "锚点拒收", "靶机")
    run = ledger.start_run(conn, task.id)
    # snapshot 空指针的行经官门造不出（append 拒空）——R0 的"snapshot 非空"是
    # 纵深防线：这里直 INSERT 一条历史/异常空壳行，证明 r0 仍守得住边界。
    conn.execute(
        "INSERT INTO observations (id, task_id, run_id, channel, meta_json, snapshot_ref, ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("obs_ghost_row", task.id, run.id, "http", "{}", "", 0),
    )
    no_snapshot = ledger.append_finding(conn, task.id, "空指针锚", "d", "obs:obs_ghost_row")
    assert r0(conn, no_snapshot) is False

    ghost = ledger.append_finding(conn, task.id, "查无此行", "d", "obs:not-a-real-id")
    assert r0(conn, ghost) is False

    prefix_only = ledger.append_finding(conn, task.id, "裸前缀", "d", "obs:")
    assert r0(conn, prefix_only) is False


def test_r0_file_anchor_unchanged(tmp_path: Path) -> None:
    """原判据不回退：真文件非空 → True；缺文件 → False（204 例老行为守恒）。"""
    good = tmp_path / "good.txt"
    good.write_text("证据", encoding="utf-8")
    conn = migrate_store.connect(":memory:")
    finding = ledger.append_finding(conn, "t1", "文件锚", "d", str(good))
    assert r0(conn, finding) is True
    missing = ledger.append_finding(conn, "t1", "缺文件锚", "d", str(tmp_path / "ghost.txt"))
    assert r0(conn, missing) is False


def test_obs_anchored_finding_verified_full_chain() -> None:
    """判票闭环前哨：obs 锚 finding 经 sweep R0 放行 → R4 oracle 命中 → verified。"""
    conn = migrate_store.connect(":memory:")
    task = ledger.start_task(conn, "判票演示", "靶机")
    run = ledger.start_run(conn, task.id)
    obs = observations.append(conn, task.id, run.id, "http", {"mock": True}, "raw/http_2.txt")
    finding = ledger.append_finding(conn, task.id, "注入命中", "describe", f"obs:{obs.id}")

    profile = JudgeProfile(enabled=True, ground_truth=(GroundTruth(id="GT-SQLI", pattern="注入"),))
    verdicts = sweep(conn, task.id, profile=profile)
    assert len(verdicts) == 1
    assert verdicts[0].id == finding.id
    assert verdicts[0].verify_state == "verified"
    assert verdicts[0].method == "oracle"


# ---------- 观测写入口基础校验 --------------------------------------------------------


def test_observations_rejects_empty_channel() -> None:
    """channel/snapshot_ref 是证据语义字段，空值在官门口拒绝（不落违例行）。"""
    conn = migrate_store.connect(":memory:")
    task = ledger.start_task(conn, "校验演示", "靶机")
    run = ledger.start_run(conn, task.id)
    with pytest.raises(LedgerError):
        observations.append(conn, task.id, run.id, "", {}, "raw/x.txt")
    with pytest.raises(LedgerError):
        observations.append(conn, task.id, run.id, "http", {}, "  ")


def test_note_for_honesty() -> None:
    """装配注记一句话说真话：关闭/开启两态可读。"""
    assert note_for(ProbeConfig()) == "探针关闭（enable_http_probe=false，零开销）"
    assert "开启" in note_for(ProbeConfig(enabled=True, source="configs/profile.toml"))
