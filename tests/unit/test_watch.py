"""watch 实时视图测试：事件渲染成可读交互日志。"""

from __future__ import annotations

import json

from secpt.cli.watch import render_event, render_lines


def _ev(seq, etype, payload):
    return {"seq": seq, "type": etype, "payload": payload, "ts": 0.0}


class TestRenderEvent:
    def test_step_boundary(self):
        r = render_event(_ev(1, "step", {"n": 12, "elapsed_s": 45.2}))
        assert "step 12" in r and "45.2s" in r

    def test_thinking(self):
        r = render_event(
            _ev(
                2,
                "assistant",
                {"reasoning": "登录接口没有验证码，可以先爆破", "content": "", "tool_calls": []},
            )
        )
        assert "💭 思考" in r and "验证码" in r

    def test_tool_call_and_result(self):
        r1 = render_event(_ev(3, "tool_call", {"tool": "bash", "args": '{"command": "ls"}'}))
        assert "🔧 bash" in r1 and "ls" in r1
        r2 = render_event(_ev(4, "tool_result", {"status": "ok", "preview": "app.py"}))
        assert "📥" in r2 and "app.py" in r2
        r3 = render_event(_ev(5, "tool_result", {"status": "error", "preview": "Error: x"}))
        assert "⚠️" in r3

    def test_answer_finding_compact(self):
        assert "🏁" in render_event(_ev(6, "assistant", {"content": "ANSWER: FLAG{a}"}))
        assert "★ 发现" in render_event(
            _ev(7, "finding", {"kind": "credential", "value": "password=x"})
        )
        assert "🗜 压缩" in render_event(_ev(8, "compaction", {"k": 2, "removed_messages": 30}))
        assert "📌 状态" in render_event(_ev(9, "status_change", {"status": "succeeded"}))

    def test_structural_events_hidden(self):
        assert render_event(_ev(10, "round_end", {"round": 1})) is None


class TestRenderLines:
    def test_incremental(self, tmp_path):
        p = tmp_path / "events.jsonl"
        events = [
            _ev(1, "step", {"n": 1, "elapsed_s": 1.0}),
            _ev(2, "tool_call", {"tool": "bash", "args": "ls"}),
            _ev(3, "tool_result", {"status": "ok", "preview": "x"}),
        ]
        p.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")
        lines, last = render_lines(p, 0)
        assert last == 3 and len(lines) == 3
        # 增量：从 seq 3 之后只有新事件
        lines2, last2 = render_lines(p, 3)
        assert lines2 == [] and last2 == 3


class TestExportLog:
    def _mk_events(self, tmp_path):
        import json as _json

        p = tmp_path / "events.jsonl"
        events = [
            _ev(1, "step", {"n": 1, "elapsed_s": 0.5}),
            _ev(2, "assistant", {"reasoning": "先侦察", "content": "开工", "tool_calls": []}),
            _ev(3, "tool_call", {"tool": "bash", "args": "ls"}),
            _ev(4, "tool_result", {"status": "ok", "preview": "app.py"}),
            _ev(5, "user_msg", {"kind": "steer", "content": "【人工操舵】换方向"}),
            _ev(6, "status_change", {"status": "succeeded"}),
        ]
        p.write_text(
            "\n".join(_json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8"
        )
        return p

    def test_export_txt(self, tmp_path, monkeypatch):
        from secpt.cli.watch import export_log

        monkeypatch.setattr(
            "secpt.cli.watch._job_events_path", lambda jid: self._mk_events(tmp_path)
        )
        out = export_log("j1", tmp_path / "run.log")
        text = out.read_text(encoding="utf-8")
        assert "step 1" in text and "🔧 bash" in text and "🧭 [steer]" in text
        assert "succeeded" in text

    def test_export_md_has_meta(self, tmp_path, monkeypatch):
        from secpt.cli.watch import export_log

        monkeypatch.setattr(
            "secpt.cli.watch._job_events_path", lambda jid: self._mk_events(tmp_path)
        )
        out = export_log("j1", tmp_path / "run.md", fmt="md")
        text = out.read_text(encoding="utf-8")
        assert text.startswith("# 任务执行日志") and "事件总数：6" in text

    def test_export_json_raw(self, tmp_path, monkeypatch):
        from secpt.cli.watch import export_log

        src = self._mk_events(tmp_path)
        monkeypatch.setattr("secpt.cli.watch._job_events_path", lambda jid: src)
        out = export_log("j1", tmp_path / "run.json", fmt="json")
        import json as _json

        rows = [_json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 6 and rows[0]["type"] == "step"

    def test_export_missing_raises(self, tmp_path, monkeypatch):
        import pytest

        from secpt.cli.watch import export_log

        monkeypatch.setattr("secpt.cli.watch._job_events_path", lambda jid: tmp_path / "nope.jsonl")
        with pytest.raises(FileNotFoundError):
            export_log("nope", tmp_path / "x.log")
