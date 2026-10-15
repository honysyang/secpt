"""并行跑分器测试：选题排序/断点跳过/意图预算/并行参数。

含 P0-1 运行期可信契约测试：状态同步变量必须真正落到 os.environ（而非只写进
objective 文案），api.py 运行期据此读账本——用 subprocess 实跑 api.py 断言
holders 非空/所有权正确、在途题 close 被拒（门反转）。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

from secpt.core.job.models import TaskBudget, TaskSpec
from secpt.core.orchestrator import benchmark as bm
from secpt.core.orchestrator.frontier import Frontier, _carry_ws_artifacts
from secpt.core.orchestrator.ledger import ChallengeLedger


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

        assert captured["max_workers"] == 2  # 平台上限 3 → 并发 2 留 1 槽余量（防结构性 409）
        assert captured["profile"] == "benchmark"
        # ②/③ 接线：settle 回收容器回调 + 账本广播载体已传入 frontier
        assert callable(captured.get("on_settled"))
        # P0-2 接线：意图「真正开跑」回调（claim 只置 claimed，running 推迟到此）
        assert callable(captured.get("on_started"))
        assert captured.get("scope_ledger") is not None
        fr = Frontier(tmp_path / "f.db")
        intents = fr.list_all(status="open")
        assert len(intents) == 4
        # P1 难度感知预算：easy=15 / medium=25 / hard=40
        assert all(i["max_steps"] == bm.DIFF_STEP_BUDGET["easy"] for i in intents)
        assert all("a-" in i["title"] for i in intents)
        assert summary["challenges_total"] == 4
        # 账本结算：fake_frontier 无 done chain → 4 题全部标 exhausted（可重试）
        assert summary["ledger"].get("exhausted") == 4

    def test_all_completed_short_circuit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setattr(bm, "fetch_challenges", lambda: [_challenge("d", completed=True)])
        spec = TaskSpec(job_id="b2", task_type="benchmark", objective="o", budget=TaskBudget())
        summary = _run(bm.run_benchmark(spec, frontier=Frontier(tmp_path / "f2.db")))
        assert summary["flags"] == [] and "无未完成" in summary["chains"][0]["intent"]

    def test_api_template_injected_into_objective(self, tmp_path, monkeypatch):
        """P0-5：api.py 模板内联进 objective（worker 首步落盘，不再探索/重写）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setattr(bm, "fetch_challenges", lambda: [_challenge("a-1", level=1)])
        captured = {}

        async def fake_frontier(objective, **kw):
            captured["objective"] = objective
            return {
                "job_id": "x",
                "mode": "frontier",
                "flags": [],
                "chains": [],
                "open_remaining": 0,
                "wall_s": 1.0,
            }

        monkeypatch.setattr(bm, "run_frontier", fake_frontier)
        spec = TaskSpec(job_id="b3", task_type="benchmark", objective="跑分", budget=TaskBudget())
        _run(bm.run_benchmark(spec, frontier=Frontier(tmp_path / "f3.db")))
        obj = captured["objective"]
        assert "平台客户端脚本" in obj and "def main" in obj  # 模板已内联
        assert "api.py close" in obj  # 模板含 close 用法（每题必 close）

    def test_fetch_requires_env(self, monkeypatch):
        monkeypatch.delenv("BENCHMARK_TOKEN", raising=False)
        monkeypatch.delenv("BENCHMARK_BASE_URL", raising=False)
        try:
            bm.fetch_challenges()
        except RuntimeError as e:
            assert "BENCHMARK" in str(e)
        else:  # pragma: no cover
            raise AssertionError("应报凭证缺失")


class TestStateEnvExport:
    """P0-1 契约：写进 objective 的状态同步变量必须同时落到 os.environ。"""

    def test_export_state_env_absolute(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        led = ChallengeLedger(tmp_path / "led.sqlite", worker_id="w-test")
        bm._export_state_env(led)
        # 绝对路径（防 bash 子进程 cwd 漂移到 ws/ 后相对路径解析错位）
        assert os.environ["SECPT_LEDGER_DB"] == str((tmp_path / "led.sqlite").resolve())
        assert os.environ["SECPT_WORKER_ID"] == "w-test"
        assert os.environ["SECPT_VAR"] == str(tmp_path.resolve())

    def test_api_py_holders_contract(self, tmp_path, monkeypatch):
        """subprocess 实跑 api.py：env 导出后 holders 非空且所有权正确、在途题 close 被拒。

        这是「提示词声明 ≠ 真相，env/账本才是真相」的运行时验证——1048/1053 的缺陷
        正是变量只写 objective 未进 env（holders 恒空、close 门 fail-open）。
        """
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        led = ChallengeLedger(tmp_path / "led.sqlite", worker_id="w-test")
        led.ensure("g-inflight", flags_total=1)
        assert led.claim("g-inflight")  # 本 worker 持有，租约有效 → 在途
        led.ensure("g-orphan")  # queued（无租约）= 孤儿
        other = ChallengeLedger(tmp_path / "led.sqlite", worker_id="w-other")
        other.ensure("g-other")
        assert other.claim("g-other")  # 其他 worker 持有 → owned=False

        api = tmp_path / "api.py"
        api.write_text(bm.API_PY_TEMPLATE, encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "SECPT_LEDGER_DB": str((tmp_path / "led.sqlite").resolve()),
                "SECPT_WORKER_ID": "w-test",
                "SECPT_VAR": str(tmp_path.resolve()),
                "BENCHMARK_BASE_URL": "http://127.0.0.1:1",  # close 被拒时不会真的联网
                "BENCHMARK_TOKEN": "dummy-token",
            }
        )

        def _run_api(*args):
            p = subprocess.run(
                [sys.executable, str(api), *args],
                cwd=str(tmp_path),
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert p.returncode == 0, p.stderr
            return json.loads(p.stdout)

        holders = _run_api("holders")["holders"]
        assert "g-inflight" in holders  # env 导出后账本可读（此前恒空）
        assert holders["g-inflight"]["owned"] is True
        assert holders["g-inflight"]["status"] == "claimed"
        assert holders["g-other"]["owned"] is False  # 他进程持有 → 非本 worker
        assert "g-orphan" not in holders  # queued 无租约 → 不出现在占用快照

        # close 门反转：在途题（有效租约）一律拒绝，交由编排层 settle 回收
        denied = _run_api("close", "g-inflight")
        assert denied["error"] == "close_denied"


class TestResumeCarry:
    """P1-1 契约：进展感知续步必须承接旧工作区产物（否则"勿推倒重来"是空话）。"""

    def test_carry_artifacts(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.core.job.resume import jobs_dir

        old_ws = jobs_dir() / "j1" / "ws"
        (old_ws / "sub").mkdir(parents=True)
        (old_ws / "exploit.py").write_text("print(1)\n", encoding="utf-8")
        (old_ws / "sub" / "data.bin").write_bytes(b"\x00\x01\x02")
        (old_ws / "transcript.json").write_text("{}", encoding="utf-8")
        (old_ws / "outputs").mkdir()
        (old_ws / "outputs" / "bash-1.log").write_text("log", encoding="utf-8")

        manifest = _carry_ws_artifacts("j1", "j1-r2")
        new_ws = jobs_dir() / "j1-r2" / "ws"
        assert (new_ws / "exploit.py").is_file()  # 产物已承接
        assert (new_ws / "sub" / "data.bin").is_file()  # 目录结构一并复制
        assert not (new_ws / "transcript.json").exists()  # 不覆盖续步会话
        assert not (new_ws / "outputs" / "bash-1.log").exists()  # bash 日志不搬运
        assert "exploit.py" in manifest and "data.bin" in manifest
        assert "transcript" not in manifest

    def test_carry_missing_ws(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        assert _carry_ws_artifacts("nope", "nope-r2") == "（无）"
