"""secpt tui 单元测试：_wrap 折行 / detail_lines 详情纯函数 / _Ledger
增量 tail / watch 非 tty 回退。curses 主循环本身不在测试范围（需要真
终端），纯函数与账本读取已覆盖主要路径与边界。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pa.infra import evidence
from pa.models import Event, now
from pa.report import tui


def _ev(seq: int, type_: str, payload: dict, mission_id: str = "m1") -> Event:
    """账本事件的最小构造：ts 用 now() 成员新值即可（详情只读展示）。"""
    return Event(
        seq=seq,
        mission_id=mission_id,
        ts=now(),
        type=type_,
        actor="test",
        payload=payload,
        idem_key=f"k{seq}",
    )


def _write(root: Path, evs: list[Event]) -> None:
    """按账本协议逐行落事件（jsonl）。"""
    p = root / "events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for ev in evs:
            f.write(ev.model_dump_json() + "\n")


# ─────────────────────────── _wrap ───────────────────────────


class TestWrap:
    def test_word_split(self):
        assert tui._wrap("aaa bbb", 7) == ["aaa bbb"]
        assert tui._wrap("aaa bbb", 6) == ["aaa", "bbb"]

    def test_hard_cut_no_whitespace(self):
        # 无空白的超长 token 只能硬切，行宽恰好 = width
        assert tui._wrap("x" * 10, 4) == ["xxxx", "xxxx", "xx"]

    def test_empty_and_multiline(self):
        assert tui._wrap("", 5) == [""]
        assert tui._wrap("a\nb", 5) == ["a", "b"]

    def test_degenerate_width(self):
        # width<=1 退化：每个字符一行，绝不死循环
        assert tui._wrap("ab", 0) == ["a", "b"]


# ─────────────────────────── detail_lines ───────────────────────────


class TestDetailLines:
    def test_state_changed_shows_judge_reason(self):
        # watch 丢弃的头号信息：judge 裁决 reason 必须顶置

        ev = _ev(
            7,
            "task.state_changed",
            {
                "task_id": "t1",
                "from": "running",
                "to": "failed",
                "attempt": 1,
                "reason": "virtual_success",
            },
        )
        lines = tui.detail_lines(ev, "/tmp", 80)
        text = "\n".join(lines)
        assert "裁决/原因: virtual_success" in text
        assert "running → failed" in text

    def test_action_started_fields(self):
        # expect/rationale/args 思考链字段全量可见
        ev = _ev(
            3,
            "action.started",
            {
                "tool": "shell",
                "args": {"cmd": "nmap -p 80 10.0.0.5"},
                "expect": "开放端口列表",
                "rationale": "先摸面",
            },
        )
        lines = tui.detail_lines(ev, "/tmp", 80)
        text = "\n".join(lines)
        assert "nmap -p 80 10.0.0.5" in text
        assert "开放端口列表" in text

    def test_finished_event_reads_evidence(self, tmp_path: Path):
        # action.finished 只带 evidence 引用 → 详情必须直读证据盘

        content = {
            "tool": "shell",
            "args": {"cmd": "ls"},
            "exit": 0,
            "stdout": "A" * 2000,
            "stderr": "B" * 400,
            "task_id": "t1",
        }
        eid = evidence.put(
            tmp_path, json.dumps(content, ensure_ascii=False, sort_keys=True), "t1"
        )
        ev = _ev(9, "action.finished", {"tool": "shell", "exit": 0, "evidence": eid})
        lines = tui.detail_lines(ev, tmp_path, 60)
        text = "\n".join(lines)

        assert eid in text
        assert f"全文见 evidence/{eid}" in text
        assert f"截断：共 {len(content['stdout'])} 字符" in text

    def test_missing_evidence_is_observable(self, tmp_path: Path):
        # 证据缺失不静默：面板显示可读说明

        ev = _ev(4, "evidence.stored", {"id": "ev-ffffffffffffffff", "task_id": "t1"})
        lines = tui.detail_lines(ev, tmp_path, 60)
        assert "暂不可读" in "\n".join(lines)

    def test_nested_payload_pretty_json(self):
        # 嵌套结构（clue 等）进面板的是完整值而非被截一百的后半截
        clue = {"kind": "port", "key": "80/tcp", "value": "open"}
        ev = _ev(5, "clue.added", {"clue": clue, "task_id": "t1"})
        text = "\n".join(tui.detail_lines(ev, "/tmp", 70))
        assert '"80/tcp"' in text

    def test_mission_finished_report_line(self):
        ev = _ev(12, "mission.finished", {"reason": "completed", "summary": "达成"})
        text = "\n".join(tui.detail_lines(ev, "/tmp", 70))
        assert "报告: /tmp/report.md" in text


# ─────────────────────────── _Ledger ───────────────────────────


class TestLedger:
    def test_poll_incremental(self, tmp_path: Path):
        led = tui._Ledger(tmp_path / "events.jsonl")
        assert led.poll() == 0  # 目录未建：0 而不是抛错
        _write(
            tmp_path,
            [
                _ev(1, "task.dispatched", {"task_id": "t9"}),
                _ev(2, "budget.charged", {"tokens": 100, "steps": 1}),
            ],
        )
        assert led.poll() == 2
        assert led.poll() == 0  # 无新增
        _write(tmp_path, [_ev(3, "worker.returned", {"task_id": "t9", "status": "ok"})])
        assert led.poll() == 1

    def test_counters_durs_finished(self, tmp_path: Path):
        _write(
            tmp_path,
            [
                _ev(1, "task.dispatched", {"task_id": "t9"}),
                _ev(2, "budget.charged", {"tokens": 500, "steps": 1}),
                _ev(3, "worker.returned", {"task_id": "t9", "status": "ok"}),
                _ev(4, "mission.finished", {"reason": "completed", "summary": "达成"}),
            ],
        )
        led = tui._Ledger(tmp_path / "events.jsonl")
        led.poll()

        assert led.counters.steps == 1
        assert led.counters.tokens == 500
        assert 0 <= led.durs[3] <= 50  # dispatched→returned 几乎同刻 ⇒ ~0ms
        assert led.finished == {"reason": "completed", "summary": "达成"}
        assert led.step == 1
        assert led.exit_code == 0  # completed → 0（退出码契约）

    def test_exit_code_budget(self, tmp_path: Path):
        _write(tmp_path, [_ev(1, "mission.finished", {"reason": "budget_steps"})])
        led = tui._Ledger(tmp_path / "events.jsonl")
        led.poll()
        assert led.exit_code == 2

    def test_budget_terminal_missing_exit_reason(self, tmp_path: Path):
        # 未收口：None 而非 0——避免静默按"正常"退出
        _write(tmp_path, [_ev(1, "task.created", {"task": {"goal": "x"}})])
        led = tui._Ledger(tmp_path / "events.jsonl")
        led.poll()
        assert led.exit_code is None


# ─────────────────────────── watch（非 tty 回退） ───────────────────────────


class TestWatchFallback:
    def test_non_tty_falls_back_to_follow(self, tmp_path: Path, monkeypatch):
        _write(tmp_path, [_ev(1, "task.created", {"task": {"goal": "x"}})])
        calls: list[str] = []

        def fake_watch(root, **kw):
            calls.append(str(root))
            return 7

        monkeypatch.setattr(tui.follow, "watch", fake_watch)

        monkeypatch.setattr(tui, "_is_tty", lambda: False)
        code = tui.watch(tmp_path, poll=0.01)
        assert code == 7
        assert calls == [str(tmp_path)]

    def test_absent_ledger_times_out(self, tmp_path: Path):
        # 既无账本又非 tty：按超时报错（SecptError），不静默吞
        from pa.errors import SecptError

        with pytest.raises(SecptError):
            tui.watch(tmp_path / "no-such", poll=0.01, wait=0.05)
