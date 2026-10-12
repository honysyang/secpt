"""并行跑分器测试：选题排序/断点跳过/意图预算/并行参数。"""

from __future__ import annotations

import asyncio

from secpt.core.job.models import TaskBudget, TaskSpec
from secpt.core.orchestrator import benchmark as bm
from secpt.core.orchestrator.frontier import Frontier


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _challenge(uc, *, level=1, difficulty="easy", done=0, total=1, completed=False):
    return {
        "unique_code": uc,
        "description": "",
        "difficulty": difficulty,
        "level": level,
        "total_score": 100,
        "flag_count": total,
        "correct_flag_count": done,
        "is_completed": completed,
        "container_status": "stopped",
        "container_addr": [],
    }


class TestSelection:
    def test_skip_completed_and_sort(self):
        rows = [
            _challenge("hard-1", level=2, difficulty="hard"),
            _challenge("easy-2", level=1, difficulty="easy", done=1, total=2),
            _challenge("easy-1", level=1, difficulty="easy"),
            _challenge("done-1", completed=True),
        ]
        picked = bm._sorted_open(rows)
        codes = [c["unique_code"] for c in picked]
        assert "done-1" not in codes
        # 先补已得分的（easy-2），再全新 easy
        assert codes[0] == "easy-2"
        assert codes.index("hard-1") > codes.index("easy-1")


class TestParallelRun:
    def test_intents_seeded_with_budget(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setattr(
            bm,
            "fetch_challenges",
            lambda: [
                _challenge("a-1", level=1),
                _challenge("a-2", level=1),
                _challenge("a-3", level=2),
                _challenge("a-4", level=3),
            ],
        )
        captured = {}

        async def fake_frontier(objective, **kw):
            captured.update(kw)
            return {
                "job_id": "x",
                "mode": "frontier",
                "flags": [],
                "chains": [],
                "open_remaining": 0,
                "wall_s": 1.0,
            }

        monkeypatch.setattr(bm, "run_frontier", fake_frontier)
        spec = TaskSpec(job_id="b1", task_type="benchmark", objective="跑分", budget=TaskBudget())
        summary = _run(bm.run_benchmark(spec, frontier=Frontier(tmp_path / "f.db")))

        assert captured["max_workers"] == 3  # 并行 3 题
        assert captured["profile"] == "benchmark"
        fr = Frontier(tmp_path / "f.db")
        intents = fr.list_all(status="open")
        assert len(intents) == 4
        assert all(i["max_steps"] == bm.PER_CHALLENGE_STEP_BUDGET for i in intents)
        assert all("a-" in i["title"] for i in intents)
        assert summary["challenges_total"] == 4

    def test_all_completed_short_circuit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setattr(bm, "fetch_challenges", lambda: [_challenge("d", completed=True)])
        spec = TaskSpec(job_id="b2", task_type="benchmark", objective="o", budget=TaskBudget())
        summary = _run(bm.run_benchmark(spec, frontier=Frontier(tmp_path / "f2.db")))
        assert summary["flags"] == [] and "无未完成" in summary["chains"][0]["intent"]

    def test_fetch_requires_env(self, monkeypatch):
        monkeypatch.delenv("BENCHMARK_TOKEN", raising=False)
        monkeypatch.delenv("BENCHMARK_BASE_URL", raising=False)
        try:
            bm.fetch_challenges()
        except RuntimeError as e:
            assert "BENCHMARK" in str(e)
        else:  # pragma: no cover
            raise AssertionError("应报凭证缺失")
