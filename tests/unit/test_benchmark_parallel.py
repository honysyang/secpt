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
    def test_workers_env_override(self, tmp_path, monkeypatch):
        """并发数 env 可配（赛前按时限/平台状态调 SECPT_BENCHMARK_WORKERS）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("SECPT_BENCHMARK_WORKERS", "2")
        monkeypatch.setattr(bm, "fetch_challenges", lambda: [_challenge("w-1")])
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
        spec = TaskSpec(job_id="bw", task_type="benchmark", objective="o", budget=TaskBudget())
        _run(bm.run_benchmark(spec, frontier=Frontier(tmp_path / "fw.db")))
        assert captured["max_workers"] == 2

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

        assert captured["max_workers"] == 3  # 默认打满平台容器上限（c-06 型死锁由
        # worker 纪律"不自 close"条款治理，不用降并发换；留槽跑 SECPT_BENCHMARK_WORKERS=2）
        # 清单型任务关 planner（复评修复）：max_intents 钉死为预置数——防队列排空后
        # 规划层给已完成题产"幽灵意图"（容器全关任务还在跑的直接原因）
        assert captured.get("max_intents") == 4
        assert captured["profile"] == "benchmark"
        # ②/③ 接线：settle 回收容器回调 + 账本广播载体已传入 frontier
        assert callable(captured.get("on_settled"))
        # P0-2 接线：意图「真正开跑」回调（claim 只置 claimed，running 推迟到此）
        assert callable(captured.get("on_started"))
        assert captured.get("scope_ledger") is not None
        fr = Frontier(tmp_path / "f.db")
        intents = fr.list_all(status="open")
        assert len(intents) == 4
        # P1 难度感知预算：easy=40 / medium=60 / hard=80（tsec1-run-1213 复盘上调，
        # 15 步连脚手架+冷启动都不够）
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

    def test_api_py_health_probe(self, tmp_path, monkeypatch):
        """A3：health 子命令返回三重探针（VPN/平台）且不抛异常。"""
        api = tmp_path / "api.py"
        api.write_text(bm.API_PY_TEMPLATE, encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "BENCHMARK_BASE_URL": "http://127.0.0.1:1",  # 平台不可达
                "BENCHMARK_TOKEN": "dummy",
                "SECPT_VPN_PROBE_URL": "http://127.0.0.1:1",  # VPN 探针不可达
            }
        )
        p = subprocess.run(
            [sys.executable, str(api), "health"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert p.returncode == 0, p.stderr
        out = json.loads(p.stdout)
        assert out == {"vpn": False, "platform": False, "ended": False}

    def test_api_py_wait_adaptive(self, tmp_path):
        """api.py wait：端口未就绪立即放弃（不盲等整 40s）、就绪即刻返回。

        时间对冲契约：1323 实测 120 次 `sleep 20` 盲等 = 整场 40+ 分钟纯墙钟，
        wait 用 2s TCP 轮询替代——就绪探测必须秒级返回。
        """
        import socket as _socket
        import time as _time

        api = tmp_path / "api.py"
        api.write_text(bm.API_PY_TEMPLATE, encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "BENCHMARK_BASE_URL": "http://127.0.0.1:1",
                "BENCHMARK_TOKEN": "dummy",
                "SECPT_WAIT_S": "4",  # 测试压缩超时窗
            }
        )

        def _wait(addr):
            t0 = _time.time()
            p = subprocess.run(
                [sys.executable, str(api), "wait", addr],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert p.returncode == 0, p.stderr
            return json.loads(p.stdout), _time.time() - t0

        # ① 无人监听：到超时窗后返回 ready=false（4s 量级，不是 40s/无限）
        out, took = _wait("127.0.0.1:1")
        assert out["ready"] is False and took < 15

        # ② 监听就绪：秒级返回 ready=true
        srv = _socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        port = srv.getsockname()[1]
        try:
            out, took = _wait(f"127.0.0.1:{port}")
        finally:
            srv.close()
        assert out["ready"] is True
        assert out["waited_s"] < 5 and took < 10


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


class TestPlatformErrors:
    """平台级失败：快速失败的清晰报错（此前 HTTPError 裸抛 traceback 崩溃）。"""

    def test_task_finished_409_actionable(self, monkeypatch):
        import json as _json
        import urllib.error

        from secpt.core.orchestrator import benchmark as B

        monkeypatch.setenv("BENCHMARK_BASE_URL", "https://tsec.invalid")
        monkeypatch.setenv("BENCHMARK_TOKEN", "tk")

        class _FP:
            def read(self):
                return _json.dumps(
                    {"code": "invalid_state", "message": "task task_x already finished"}
                ).encode()

        def _raise(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, _FP())

        monkeypatch.setattr("urllib.request.urlopen", _raise)
        try:
            B.fetch_challenges()
            raise AssertionError("必须抛 BenchmarkUnavailable")
        except B.BenchmarkUnavailable as e:
            msg = str(e)
            assert "任务已结束" in msg and "新任务" in msg and "BENCHMARK_TASK_ID" in msg

    def test_network_error_hint_vpn(self, monkeypatch):
        import urllib.error

        from secpt.core.orchestrator import benchmark as B

        monkeypatch.setenv("BENCHMARK_BASE_URL", "https://tsec.invalid")
        monkeypatch.setenv("BENCHMARK_TOKEN", "tk")

        def _raise(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr("urllib.request.urlopen", _raise)
        try:
            B.fetch_challenges()
            raise AssertionError("必须抛 BenchmarkUnavailable")
        except B.BenchmarkUnavailable as e:
            assert "VPN" in str(e)

    def test_ledger_task_scoped_by_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.core.orchestrator.ledger import ChallengeLedger

        monkeypatch.setenv("BENCHMARK_TASK_ID", "task_new123")
        led = ChallengeLedger()
        assert led.db_path.name == "benchmark_ledger_task_new123.sqlite"
        monkeypatch.delenv("BENCHMARK_TASK_ID")
        led2 = ChallengeLedger()
        assert led2.db_path.name == "benchmark_ledger.sqlite"  # 缺省不破坏旧行为


class TestRetryIntelligence:
    """复评修复：预算×旗数 与 重试失败上下文注入。"""

    @staticmethod
    def _fake_frontier():
        async def fake_frontier(objective, **kw):
            return {
                "job_id": "x",
                "mode": "frontier",
                "flags": [],
                "chains": [],
                "open_remaining": 0,
                "wall_s": 1.0,
            }

        return fake_frontier

    def test_budget_scales_with_flag_count(self, tmp_path, monkeypatch):
        """A1：多旗题按 2 旗/段拆意图；每段预算=难度基线，总预算与旧倍率口径一致。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setattr(
            bm,
            "fetch_challenges",
            lambda: [
                _challenge("s-1", difficulty="medium", total=1),
                _challenge("m-4", difficulty="medium", total=4),
                _challenge("h-6", difficulty="hard", total=6),
            ],
        )
        monkeypatch.setattr(bm, "run_frontier", self._fake_frontier())
        spec = TaskSpec(job_id="bs", task_type="benchmark", objective="o", budget=TaskBudget())
        _run(bm.run_benchmark(spec, frontier=Frontier(tmp_path / "fs.db")))
        segs: dict[str, list[int]] = {}
        for i in Frontier(tmp_path / "fs.db").list_all(status="open"):
            key = i["title"].split("（")[0].replace("通关 ", "")
            segs.setdefault(key, []).append(i["max_steps"])
        assert segs["s-1"] == [bm.DIFF_STEP_BUDGET["medium"]]  # 单旗单段
        assert segs["m-4"] == [bm.DIFF_STEP_BUDGET["medium"]] * 2  # 4 旗 → 2 段
        assert segs["h-6"] == [bm.DIFF_STEP_BUDGET["hard"]] * 3  # 6 旗 → 3 段

    def test_retry_digest_injected_into_goal(self, tmp_path, monkeypatch):
        """上一轮失败摘要（settle 台账）注入本轮 goal——重试带记忆换路线。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("BENCHMARK_TASK_ID", "task_x")
        from secpt.core.orchestrator.frontier import settle_digest_key

        title = "通关 r-1（easy L1）"
        d = tmp_path / "knowledge" / "settle_digests"
        d.mkdir(parents=True)
        (d / f"{settle_digest_key(title)}.json").write_text(
            json.dumps(
                {
                    "title": title,
                    "status": "exhausted",
                    "steps": 30,
                    "fail_reason": "超意图步数预算（15 步硬止损）",
                    "tail": "尝试 union 注入被 WAF 拦截，未试编码变形",
                    "ts": 1.0,
                    "base": "old-run",
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(bm, "fetch_challenges", lambda: [_challenge("r-1", level=1)])
        monkeypatch.setattr(bm, "run_frontier", self._fake_frontier())
        spec = TaskSpec(job_id="bg", task_type="benchmark", objective="o", budget=TaskBudget())
        _run(bm.run_benchmark(spec, frontier=Frontier(tmp_path / "fg.db")))
        goals = [i["goal"] for i in Frontier(tmp_path / "fg.db").list_all(status="open")]
        assert any("上一轮尝试记录" in g and "WAF 拦截" in g for g in goals)

    def test_settle_digest_roundtrip_and_task_isolation(self, tmp_path, monkeypatch):
        """台账写入/读回口径一致；指纹按 BENCHMARK_TASK_ID 隔离（防跨任务串摘要）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("BENCHMARK_TASK_ID", "tk1")
        from secpt.core.orchestrator.frontier import _write_settle_digest, settle_digest_key

        title = "通关 x-1（easy L1）"
        key1 = settle_digest_key(title)
        _write_settle_digest(
            "b1",
            title,
            "exhausted",
            30,
            "预算打满",
            [{"role": "assistant", "content": "最后卡点A"}],
        )
        pth = tmp_path / "knowledge" / "settle_digests" / f"{key1}.json"
        dd = json.loads(pth.read_text(encoding="utf-8"))
        assert dd["status"] == "exhausted" and dd["tail"].startswith("最后卡点")
        monkeypatch.setenv("BENCHMARK_TASK_ID", "tk2")
        assert settle_digest_key(title) != key1  # 跨任务指纹隔离


class TestSettlementVerification:
    """tsec1-run-1213 假 solved 修复：done 只代表链收尾，solved 必须凭平台实况核实。"""

    def _run_bm(self, tmp_path, monkeypatch, live, chains, job_id="sv1"):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setattr(bm, "fetch_challenges", lambda: live)

        async def fake_frontier(objective, **kw):
            return {
                "job_id": "x",
                "mode": "frontier",
                "flags": [],
                "chains": chains,
                "open_remaining": 0,
                "wall_s": 1.0,
            }

        monkeypatch.setattr(bm, "run_frontier", fake_frontier)
        spec = TaskSpec(job_id=job_id, task_type="benchmark", objective="跑分", budget=TaskBudget())
        return _run(bm.run_benchmark(spec, frontier=Frontier(tmp_path / f"{job_id}.db")))

    def test_done_without_flags_not_solved(self, tmp_path, monkeypatch):
        """链收尾但平台 0 分 → 绝不允许标 solved（a-05 事故回归）。"""
        self._run_bm(
            tmp_path,
            monkeypatch,
            live=[_challenge("a-1", level=1, done=0)],
            chains=[{"intent": "通关 a-1（easy L1）", "status": "done"}],
        )
        led = ChallengeLedger()
        assert led.status("a-1") != "solved"
        assert led.status("a-1") == "exhausted"  # 可重试

    def test_done_with_live_flags_marked_solved(self, tmp_path, monkeypatch):
        self._run_bm(
            tmp_path,
            monkeypatch,
            live=[_challenge("a-2", level=1, done=1, total=1)],
            chains=[{"intent": "通关 a-2（easy L1）", "status": "done"}],
        )
        led = ChallengeLedger()
        assert led.status("a-2") == "solved"

    def test_partial_flags_recorded_retryable(self, tmp_path, monkeypatch):
        """部分旗：不标 solved，flags_found 回填供下轮续打。"""
        self._run_bm(
            tmp_path,
            monkeypatch,
            live=[_challenge("b-2", level=1, done=1, total=2)],
            chains=[{"intent": "通关 b-2（easy L1）", "status": "done"}],
        )
        led = ChallengeLedger()
        assert led.status("b-2") == "exhausted"
        row = next(e for e in led.iter_entries() if e["unique_code"] == "b-2")
        assert row["flags_found"] == 1

    def test_fetch_failure_fail_closed(self, tmp_path, monkeypatch):
        """结算时平台核实失败 → 全部不标 solved（宁可重打不丢题）。"""
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:  # 选题阶段的 fetch 正常
                return [_challenge("a-3", level=1)]
            raise RuntimeError("platform down")  # 结算阶段的 fetch 挂

        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setattr(bm, "fetch_challenges", flaky)

        async def fake_frontier(objective, **kw):
            return {
                "job_id": "x",
                "mode": "frontier",
                "flags": [],
                "chains": [{"intent": "通关 a-3（easy L1）", "status": "done"}],
                "open_remaining": 0,
                "wall_s": 1.0,
            }

        monkeypatch.setattr(bm, "run_frontier", fake_frontier)
        spec = TaskSpec(job_id="sv4", task_type="benchmark", objective="跑分", budget=TaskBudget())
        _run(bm.run_benchmark(spec, frontier=Frontier(tmp_path / "sv4.db")))
        assert ChallengeLedger().status("a-3") != "solved"


class TestReconcileLedger:
    """对账：平台实况为准清理假 solved / 同步部分旗。"""

    def test_downgrade_false_solved_and_sync(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        led = ChallengeLedger()
        led.ensure("f-1", flags_total=1)
        led.ensure("t-1", flags_total=2)
        led.ensure("p-1", flags_total=2)
        led.ensure("n-1", flags_total=1)
        led.mark_solved("f-1", flags_found=0, flags_total=1)  # 假 solved
        led.mark_solved("t-1", flags_found=2, flags_total=2)  # 真 solved（保持）
        led.mark_failed("p-1")  #  exhausted，平台已有 1/2 旗
        led.mark_failed("n-1")  #  exhausted，平台已完成（结算漏标）
        monkeypatch.setattr(
            bm,
            "fetch_challenges",
            lambda: [
                _challenge("f-1", done=0, total=1),
                _challenge("t-1", done=2, total=2),
                _challenge("p-1", done=1, total=2),
                _challenge("n-1", done=1, total=1),
                _challenge("ghost-9", done=0),  # 旧代际，账本没有 → 忽略
            ],
        )
        rep = bm.reconcile_ledger(led)
        assert rep["downgraded"] == ["f-1"]
        assert rep["ok"] == 1  # t-1 保持 solved
        assert rep["synced"] == ["p-1"]
        assert rep["solved_now"] == ["n-1"]
        assert led.status("f-1") == "exhausted"
        assert led.status("t-1") == "solved"
        assert led.status("n-1") == "solved"
        row = next(e for e in led.iter_entries() if e["unique_code"] == "p-1")
        assert row["flags_found"] == 1


class TestApiPredeploy:
    """tsec1-run-1213 P2：on_started 把 api.py 预落盘 worker 工作区（省 1-2 步）。"""

    def test_on_started_writes_api_py(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setattr(bm, "fetch_challenges", lambda: [_challenge("a-1", level=1)])
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
        spec = TaskSpec(job_id="pd1", task_type="benchmark", objective="跑分", budget=TaskBudget())
        _run(bm.run_benchmark(spec, frontier=Frontier(tmp_path / "pd1.db")))
        on_started = captured["on_started"]
        intent = SimpleNamespace(id="i1234567ab", title="通关 a-1（easy L1）")
        on_started(intent)
        from secpt.core.job.resume import jobs_dir

        api = jobs_dir() / "pd1-i67ab" / "ws" / "api.py"
        assert api.is_file()
        assert "BENCHMARK_TOKEN" in api.read_text(encoding="utf-8")

    def test_release_claims_flag(self, tmp_path, monkeypatch):
        """--release-claims：强制释放 claimed/running 回可重试；solved 不触碰。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        led = ChallengeLedger()
        led.ensure("c-1", flags_total=1)
        led.ensure("c-2", flags_total=1)
        led.claim("c-1")
        led.mark_running("c-1")
        led.claim("c-2")
        led.mark_solved("c-2", flags_found=1, flags_total=1)
        monkeypatch.setattr(
            bm,
            "fetch_challenges",
            lambda: [_challenge("c-1"), _challenge("c-2", done=1, total=1)],
        )
        rep = bm.reconcile_ledger(led, release_claims=True)
        assert rep["released"] == 1  # 只有 c-1（claimed/running）；c-2 solved 不动
        assert led.status("c-1") == "exhausted"
        assert led.status("c-2") == "solved"
