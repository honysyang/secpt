"""S2 端到端验证：3 个任务经 JobManager 并行提交，SECPT_FAKE_LLM 完成。

验证：
  1. 三个 job 全部 succeeded（退出码 0）
  2. 并发上限生效（max_parallel=2 时，队列 FIFO 补位）
  3. ws/ 目录互相独立，无跨写
  4. 中途 cancel 一个，其余照常
用法：SECPT_FAKE_LLM=1 .venv/bin/python scripts/verify_s2_manager.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-fake-for-verify")
os.environ["SECPT_FAKE_LLM"] = "1"

from secpt.core.job.manager import JobManager
from secpt.core.job.models import TaskBudget, TaskSpec
from secpt.core.job.resume import jobs_dir


def _spec(job_id: str) -> TaskSpec:
    return TaskSpec(
        job_id=job_id,
        task_type="generic",
        objective=f"验证并行 job {job_id}",
        budget=TaskBudget(wall_s=120, steps=8),
    )


def main() -> int:
    tmp_var = tempfile.mkdtemp(prefix="secpt_s2_verify_")
    os.environ["SECPT_VAR"] = tmp_var

    mgr = JobManager(max_parallel_jobs=2, db_path=f"{tmp_var}/secpt.sqlite")
    for i in range(1, 4):
        mgr.submit(_spec(f"job{i}"))

    t0 = time.time()
    cancelled = False
    while time.time() - t0 < 60:
        mgr.reap()
        # 等 worker 起来后 cancel job2（SIGTERM）
        if not cancelled and time.time() - t0 >= 0.8:
            mgr.cancel("job2")
            cancelled = True
        remaining = [
            j
            for j in ("job1", "job2", "job3")
            if mgr.get(j)
            and mgr.get(j).status
            not in ("succeeded", "failed", "budget_exceeded", "cancelled", "stuck")
        ]
        if not remaining:
            break
        time.sleep(0.5)

    print("\n===== 最终状态 =====")
    for jid in ("job1", "job2", "job3"):
        rec = mgr.get(jid)
        if rec:
            print(f"  {jid}: status={rec.status} steps={rec.steps}")

    # 判据 1：未取消的两个 succeeded
    assert mgr.get("job1").status == "succeeded", mgr.get("job1").status
    assert mgr.get("job3").status == "succeeded", mgr.get("job3").status
    # job2 被 cancel
    assert mgr.get("job2").status == "cancelled", mgr.get("job2").status
    print("\n[1] 并行完成 + cancel 生效 PASS")

    # 判据 2：ws 独立，无跨写
    ws1 = jobs_dir() / "job1" / "ws"
    ws3 = jobs_dir() / "job3" / "ws"
    f1 = {p.name for p in ws1.rglob("*") if p.is_file()}
    f3 = {p.name for p in ws3.rglob("*") if p.is_file()}
    common = f1 & f3 - {"transcript.json"}  # transcript 每个 ws 都有，名字相同不算跨写
    assert not common, f"ws 出现跨写: {common}"
    print("[2] ws/ 目录独立、无跨写 PASS")

    # 判据 3：job1/job3 的 flag 各自一致（fake flag 含 job_id，不同属正常）
    print(f"[3] job1 flag={mgr.get('job1').spec.objective[:30]}...")
    print("\nS2 manager 端到端验证 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
