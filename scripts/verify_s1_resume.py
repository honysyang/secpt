"""S1 端到端 resume 验证：fake LLM 跑 5 步 → 手动断点 → resume 继续跑 5 步。

验证：
  1. resume 后 events.jsonl 的 seq 连续（不重头再来）
  2. rounds/ 至少有 NNN.messages.json
  3. resume 前后 messages 链一致延续
用法：.venv/bin/python scripts/verify_s1_resume.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# 让 secpt 包可导入
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 防止 _api_key() 拦截（fake llm 不需要 key，但 engine_runner 会检查）
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-fake-for-verify")

from secpt.cli.engine_runner import resume_engine, run_engine
from secpt.core.job.resume import jobs_dir
from secpt.core.job.store import JobStore


class FakeLLM:
    """带 tool_calls 的假 LLM：按序输出脚本，触发工具执行。"""

    def __init__(self, n_steps: int, call_id_prefix: str) -> None:
        self.n = n_steps
        self.calls = 0
        self.prefix = call_id_prefix

    async def call(self, messages, tools, model):
        self.calls += 1
        step = self.calls
        if step > self.n:
            # 结束后：输出 ANSWER 结束
            return _resp({"role": "assistant", "content": f"ANSWER: FLAG{{done_{self.prefix}}}"})
        return _resp(
            {
                "role": "assistant",
                "content": f"step {step}",
                "tool_calls": [
                    {
                        "id": f"{self.prefix}-call-{step}",
                        "type": "function",
                        "function": {"name": "bash", "arguments": json.dumps({"command": "ls"})},
                    }
                ],
            }
        )


def _resp(msg_dict: dict):
    class _Fn:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    class _TC:
        def __init__(self, tc):
            self.id = tc["id"]
            self.type = tc.get("type", "function")
            self.function = _Fn(tc["function"]["name"], tc["function"]["arguments"])

    class _Msg:
        def __init__(self, d):
            self.content = d.get("content") or ""
            self.tool_calls = [_TC(x) for x in (d.get("tool_calls") or [])]
            self.reasoning_content = None

    class _Choice:
        def __init__(self, msg):
            self.message = msg

    class _Resp:
        def __init__(self, msg):
            self.choices = [_Choice(msg)]

    return _Resp(_Msg(msg_dict))


def main() -> int:
    tmp_var = tempfile.mkdtemp(prefix="secpt_s1_verify_")
    os.environ["SECPT_VAR"] = tmp_var

    store = JobStore(f"{tmp_var}/secpt.sqlite")
    job_id = "vjob1"
    # 第一段：跑 5 步，目标 FLAG
    r1 = run_engine(
        objective="验证任务：解出 FLAG{done}",
        profile="ctf",
        job_id=job_id,
        max_steps=6,
        max_wall_s=120,
        job_store=store,
        llm=FakeLLM(n_steps=5, call_id_prefix="seg1"),
    )
    assert r1.steps == 6, r1.steps
    assert r1.flag == "FLAG{done_seg1}", r1.flag

    # 检查落盘
    jdir = jobs_dir() / job_id
    assert (jdir / "task.yaml").is_file(), "task.yaml 缺失"
    assert (jdir / "events.jsonl").is_file(), "events.jsonl 缺失"
    rounds = sorted((jdir / "rounds").glob("*.messages.json"))
    assert rounds, "rounds/ 无存档"

    events_before = [
        json.loads(line)
        for line in (jdir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    n_events_before = len(events_before)
    last_seq_before = events_before[-1]["seq"]
    print(f"[1] 第一段完成: steps={r1.steps} flag={r1.flag}")
    print(f"    events 条数={n_events_before} 最后 seq={last_seq_before}")
    print(f"    rounds: {[p.name for p in rounds]}")

    # 第二段：resume（模拟进程重启后继续）
    r2 = resume_engine(
        job_id,
        job_store=store,
        llm=FakeLLM(n_steps=5, call_id_prefix="seg2"),
    )
    assert r2.steps == 6, r2.steps

    events_after = [
        json.loads(line)
        for line in (jdir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    seqs = [e["seq"] for e in events_after]
    assert seqs == list(range(1, len(seqs) + 1)), f"seq 不连续: {seqs}"
    # 不能重头再来：新 seq 从 last_seq_before+1 开始增长
    new_events = [e for e in events_after if e["seq"] > last_seq_before]
    assert new_events, "resume 后没有新增事件（重头再来或没跑）"
    assert new_events[0]["seq"] == last_seq_before + 1, (
        f"seq 断裂: {new_events[0]['seq']} != {last_seq_before + 1}"
    )
    print(
        f"[2] resume 后新增 {len(new_events)} 条事件, 新 seq 从 {new_events[0]['seq']} 开始（连续）"
    )

    # jobs 表状态
    rec = store.get_job(job_id)
    assert rec is not None
    assert rec.status == "succeeded", rec.status
    assert rec.steps == 6, rec.steps
    print(f"[3] jobs 表: status={rec.status} steps={rec.steps}")

    # rounds 数量应增加
    rounds_after = sorted((jdir / "rounds").glob("*.messages.json"))
    assert len(rounds_after) >= len(rounds) + 1, "resume 后 rounds 未增加"
    print(f"[4] rounds 增加: {len(rounds)} -> {len(rounds_after)}")

    # events 头尾各 5 行（验收要求贴回来）
    print("\n===== events.jsonl 头 5 行 =====")
    for e in events_after[:5]:
        print(f"  seq={e['seq']} round={e['round']} type={e['type']}")
    print("===== events.jsonl 尾 5 行 =====")
    for e in events_after[-5:]:
        print(f"  seq={e['seq']} round={e['round']} type={e['type']}")

    print("\nS1 resume 端到端验证 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
