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
