"""S3 验收脚本（fake 模式）：SECPT_FAKE_WINDOW=8000 强制压缩+轮转多次。

判据：
  1. 仍然拿到同一个 FLAG（硬指标）        → FLAG{s3fake_done}
  2. rounds/ 下 >= 2 个 messages.json     → 确实发生了轮转
  3. events 出现 compaction 与 rollover，摘要含"已排除"段落
  4. cost 有记账（on_tokens → jobs.cost_cny）
  5. 单测已另跑（pytest tests/unit/core/context/）

用法：SECPT_FAKE_LLM=1 .venv/bin/python verify_s3.py
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

os.environ["SECPT_FAKE_LLM"] = "1"
os.environ["SECPT_FAKE_WINDOW"] = "8000"   # 假装窗口只有 8K，强制触发

from secpt.core.job.event import EventSink  # noqa: E402
from secpt.core.job.fakellm import FakeLLM  # noqa: E402
from secpt.core.job.models import JobRecord  # noqa: E402
from secpt.core.job.store import JobStore  # noqa: E402
from secpt.core.watchdog import Watchdog  # noqa: E402
from secpt.runtime.engine import Engine, RunConfig  # noqa: E402

JOB_ID = "s3fake"
EXPECTED_FLAG = "FLAG{s3fake_done}"


def _events(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        jobs_dir = base / "jobs"
        db = base / "secpt.sqlite"

        cfg = RunConfig(
            job_id=JOB_ID,
            objective="测试任务：在 fake 环境下验证压缩与轮转不丢 flag。",
            system_extra="你是测试员。",
            workspace=jobs_dir / JOB_ID,
            max_steps=120,
            max_wall_s=600,
            bash_timeout_s=5,
        )
        llm = FakeLLM(n_steps=118, job_id=JOB_ID, step_delay_s=0.005, content_len=600)

        store = JobStore(db)
        sink = EventSink(JOB_ID, jobs_dir, store=store)
        from secpt.core.job.models import TaskSpec
        rec = JobRecord(
            job_id=JOB_ID,
            spec=TaskSpec(job_id=JOB_ID, objective=cfg.objective),
        )
        store.upsert_job(rec)

        hooks = Watchdog(sink, store=store, job_id=JOB_ID)
        result = asyncio.run(Engine(cfg, llm=llm, hooks=hooks).run())

        # ---- 判据 ----
        events = _events(sink.events_path)
        rounds = sorted((jobs_dir / JOB_ID / "rounds").glob("*.messages.json")) \
            if (jobs_dir / JOB_ID / "rounds").is_dir() else []
        compactions = [e for e in events if e["type"] == "compaction"]
        rollovers = [e for e in events if e["type"] == "rollover"]
        handoffs = [e["payload"].get("handoff", "") for e in rollovers]
        pauses = [e for e in events if e["type"] == "status_change"
                  and e["payload"].get("status") == "paused"]

        final_rec = store.get_job(JOB_ID)
        cost = final_rec.cost_cny if final_rec else 0.0

        print("=" * 60)
        print(f"flag  = {result.flag}")
        print(f"steps = {result.steps}")
        print(f"wall  = {result.wall_s:.1f}s")
        print(f"cost  = {cost:.6f} CNY")
        print(f"round 文件 = {len(rounds)} 个（{rounds[:5]}...）")
        print(f"compaction 事件 = {len(compactions)} 次")
        print(f"rollover 事件 = {len(rollovers)} 次（含 paused 告警 {len(pauses)} 次）")
        print("=" * 60)

        ok = True
        # 1) 同一 FLAG
        if result.flag != EXPECTED_FLAG:
            print("[FAIL] flag 不一致：压缩/轮转丢了关键信息")
            ok = False
        else:
            print("[PASS] flag 一致")
        # 2) >= 2 个 round 文件
        if len(rounds) < 2:
            print("[FAIL] rounds/ 不足 2 个 messages.json（没发生轮转）")
            ok = False
        else:
            print("[PASS] rounds/ >= 2 个 messages.json")
        # 3) compaction + rollover 事件，摘要含"已排除"
        if not compactions:
            print("[FAIL] 无 compaction 事件")
            ok = False
        else:
            print("[PASS] compaction 事件存在")
        if not rollovers:
            print("[FAIL] 无 rollover 事件")
            ok = False
        else:
            print("[PASS] rollover 事件存在")
            sample = handoffs[0]
            has_excluded = "已排除" in sample
            print(f"[{'PASS' if has_excluded else 'FAIL'}] 摘要含'已排除'段落")
            ok = ok and has_excluded
            print("\n----- 一份 handoff 摘要原文 -----")
            print(sample)
            print("---------------------------------")
        # 4) cost 有记账
        if cost <= 0:
            print("[FAIL] cost 未记账（on_tokens 未生效）")
            ok = False
        else:
            print("[PASS] cost 记账正常")

        print("\n" + ("S3 fake 验收全部通过" if ok else "S3 fake 验收未通过"))
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
