"""A2/A3/B1/B2/C1/C2 测试：战役多轮、排空、族级 playbook、进度锚点、意图级工具。"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from secpt.core.job.models import TaskBudget, TaskSpec
from secpt.core.orchestrator import benchmark as bm
from secpt.core.orchestrator.frontier import Frontier, frontier_db_path, run_frontier
from secpt.runtime.engine import RunConfig, progress_anchor_text, write_progress_anchor


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _spec(job_id: str = "camp1") -> TaskSpec:
    return TaskSpec(job_id=job_id, task_type="benchmark", objective="o", budget=TaskBudget())


# ───────────────────────── A1：分段与标题解析 ─────────────────────────


class TestChunking:
    def test_chunk_ranges(self):
        assert bm.chunk_ranges(1) == [(1, 1)]
        assert bm.chunk_ranges(3) == [(1, 3)]  # ≤3 旗单段（保留原语义）
        assert bm.chunk_ranges(4) == [(1, 2), (3, 4)]  # 4 旗 → 2 段
        assert bm.chunk_ranges(6) == [(1, 2), (3, 4), (5, 6)]  # 6 旗 → 3 段

    def test_second_round_skips_satisfied_segments(self, tmp_path, monkeypatch):
        """A2 重试轮：平台已到账的段不再重复入队（只补未完成段 + verify）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setattr(
            bm,
            "fetch_challenges",
            lambda: [
                {
                    "unique_code": "b-09",
                    "description": "web 门户",
                    "difficulty": "hard",
                    "level": 1,
                    "total_score": 1200,
                    "flag_count": 4,
                    "correct_flag_count": 2,  # 前两旗已到账
                    "is_completed": False,
                    "container_status": "stopped",
                    "container_addr": [],
                }
            ],
        )

        async def fake_frontier(objective, **kw):
            return {
                "job_id": "x",
                "mode": "frontier",
                "flags": [],
                "chains": [],
                "open_remaining": 0,
                "wall_s": 0.1,
            }

        monkeypatch.setattr(bm, "run_frontier", fake_frontier)
        fr = Frontier(tmp_path / "f2.db")
        _run(bm.run_benchmark(_spec("skip1"), frontier=fr))
        titles = [i["title"] for i in fr.list_all()]
        assert not any("段1/2" in t for t in titles)  # 已到账段跳过
        assert any("段2/2" in t for t in titles)  # 未完成段保留
        assert any("复验" in t for t in titles)  # B1 verify 仍在

    def test_digest_injected_for_done_status(self, tmp_path, monkeypatch):
        """1323 修正：engine 诚实收尾记为 done——失败题也必须注入上一轮摘要。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.core.orchestrator.frontier import settle_digest_key

        ddir = tmp_path / "knowledge" / "settle_digests"
        ddir.mkdir(parents=True)
        title = "通关 z-1（easy L1）"
        (ddir / f"{settle_digest_key(title)}.json").write_text(
            json.dumps(
                {
                    "status": "done",
                    "steps": 42,
                    "fail_reason": "",
                    "tail": "尝试过 X 路线但未拿到 flag",
                }
            ),
            encoding="utf-8",
        )
        txt = bm._load_fail_digest(title)
        assert "尝试过 X 路线" in txt and "status=done" in txt

    def test_uc_from_title(self):
        assert bm.uc_from_title("通关 b-01（hard L1）·段1/2") == "b-01"
        assert bm.uc_from_title("复验 f2-05（hard L1）") == "f2-05"
        assert bm.uc_from_title("聚合渗透报告（report）") == ""

    def test_tools_for_family(self):
        assert "http_request" in bm.tools_for_challenge("某企业内部门户 Web 登录接口存在 SQL 注入")
        assert "analyze_binary" in bm.tools_for_challenge("从固件镜像提取 MCU 可执行文件并逆向")
        assert bm.tools_for_challenge("杂项") == ""


# ───────────────────────── A2：战役多轮 ─────────────────────────


class TestCampaign:
    def test_is_challenge_done_and_available_selection(self):
        """available（容器在跑）但未通关的题必须纳入选题；完成判定按旗数。"""
        assert bm._is_challenge_done({"is_completed": True})
        assert bm._is_challenge_done({"flag_count": 4, "correct_flag_count": 4})
        assert not bm._is_challenge_done({"flag_count": 4, "correct_flag_count": 3})
        rows = [
            {
                "unique_code": "a-1",
                "container_status": "available",
                "container_addr": ["10.0.0.1:80"],
                "flag_count": 1,
                "correct_flag_count": 0,
                "is_completed": False,
            },
            {
                "unique_code": "a-2",
                "container_status": "stopped",
                "flag_count": 1,
                "correct_flag_count": 1,
                "is_completed": True,
            },
        ]
        assert [c["unique_code"] for c in bm._sorted_open(rows)] == ["a-1"]

    def test_effective_soft_mb_anti_loop(self):
        """软着陆防抖：冷却窗口内回收过/基线已超软限 → 抬高本段阈值。"""
        # 正常：基线低、无冷却
        assert bm._effective_soft_mb(11264, 500, 0, 1000, 600, 1536) == 11264
        # 冷却窗口内刚回收过（100s 前 < 600s）→ 抬高
        assert bm._effective_soft_mb(11264, 500, 900, 1000, 600, 1536) == 12800
        # 冷却已过（900s 前）→ 不抬高
        assert bm._effective_soft_mb(11264, 500, 100, 1000, 600, 1536) == 11264
        # 首启无历史：基线高也不抬升（避免把低阈值配置误判为循环）
        assert bm._effective_soft_mb(11264, 12000, 0, 1000, 600, 1536) == 11264
        # 回收过且基线仍高于冷却后阈值 → 基线+余量
        assert bm._effective_soft_mb(11264, 13000, 900, 1000, 600, 1536) == 14536
        # 关闭软着陆（0）永远返回 0
        assert bm._effective_soft_mb(0, 12000, 900, 1000, 600, 1536) == 0

    def test_newest_event_mtime(self, tmp_path):
        """进度看门狗只统计本战役 worker 的 events.jsonl（前缀匹配、取最大 mtime）。"""
        jobs = tmp_path / "jobs"
        (jobs / "j1-i0001").mkdir(parents=True)
        (jobs / "j1-i0002").mkdir(parents=True)
        p1 = jobs / "j1-i0001" / "events.jsonl"
        p2 = jobs / "j1-i0002" / "events.jsonl"
        p1.write_text("{}\n", encoding="utf-8")
        p2.write_text("{}\n", encoding="utf-8")
        os.utime(p1, (1000, 1000))
        os.utime(p2, (2000, 2000))
        assert bm._newest_event_mtime(jobs, "j1") == 2000
        # 其他任务的 job 不能把本战役判成"有进度"
        other = jobs / "other-i0001" / "events.jsonl"
        other.parent.mkdir(parents=True)
        other.write_text("{}\n", encoding="utf-8")
        os.utime(other, (3000, 3000))
        assert bm._newest_event_mtime(jobs, "j1") == 2000
        # 无匹配 → 0（看门狗以首次扫描为基线，不会立即误杀）
        assert bm._newest_event_mtime(jobs, "missing") == 0.0

    def test_two_rounds_then_complete(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("SECPT_CAMPAIGN_ROUNDS", "3")
        calls: list[int] = []

        async def fake_run(spec, **kw):
            calls.append(1)
            return {
                "job_id": "x",
                "mode": "benchmark(parallel)",
                "flags": [],
                "chains": [],
                "open_remaining": 0,
                "wall_s": 0.1,
                "challenges_picked": 1,
                "ledger": {},
            }

        monkeypatch.setattr(bm, "run_benchmark", fake_run)
        rows = [
            [
                {
                    "unique_code": "a-1",
                    "flag_count": 1,
                    "correct_flag_count": 0,
                    "is_completed": False,
                }
            ],
            [
                {
                    "unique_code": "a-1",
                    "flag_count": 1,
                    "correct_flag_count": 0,
                    "is_completed": False,
                }
            ],
            [
                {
                    "unique_code": "a-1",
                    "flag_count": 1,
                    "correct_flag_count": 1,
                    "is_completed": True,
                }
            ],
        ]
        # v4.2：轮前对账也是平台快照消费者。按调用序返回：前 3 次=进行中
        # （对账1/轮1后判定/对账2），第 4 次起=已通关（轮 2 后判定）
        fetch_n = [0]

        def fake_fetch():
            fetch_n[0] += 1
            return rows[0 if fetch_n[0] <= 3 else 2]

        monkeypatch.setattr(bm, "fetch_challenges", fake_fetch)
        monkeypatch.setattr(
            bm,
            "_platform_snapshot",
            lambda force=False, ttl=5: {str(c["unique_code"]): c for c in bm.fetch_challenges()},
        )
        monkeypatch.setattr(
            "secpt.core.orchestrator.ledger.ChallengeLedger.release_own_claims",
            lambda self, reason="": 0,
        )
        out = _run(bm.run_benchmark_campaign(_spec()))
        # 第 1 轮结束未完成 → 第 2 轮；第 2 轮后全部通关 → 停止（不到第 3 轮）
        assert len(calls) == 2
        assert out["campaign"]["rounds_done"] == 2
        sp = bm.campaign_state_path(_spec())
        assert sp.is_file()
        state = json.loads(sp.read_text(encoding="utf-8"))
        assert [r["n"] for r in state["rounds"]] == [1, 2]

    def test_stops_when_platform_ended(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        calls: list[int] = []

        async def fake_run(spec, **kw):
            calls.append(1)
            return {
                "job_id": "x",
                "mode": "benchmark(parallel)",
                "flags": [],
                "chains": [],
                "open_remaining": 0,
                "wall_s": 0.1,
                "challenges_picked": 1,
                "ledger": {},
            }

        monkeypatch.setattr(bm, "run_benchmark", fake_run)
        monkeypatch.setattr(bm, "fetch_challenges", lambda: [])
        monkeypatch.setattr(
            bm,
            "_platform_snapshot",
            lambda force=False, ttl=5: None,
        )

        def _ended(force=False, ttl=5):
            raise bm.BenchmarkEnded("任务已结束")

        # 第一轮后判定时抛结束 → 不再进入第二轮
        monkeypatch.setattr(bm, "_platform_snapshot", _ended)
        out = _run(bm.run_benchmark_campaign(_spec()))
        assert len(calls) == 1
        assert out["campaign"]["rounds_done"] == 1

    def test_unlimited_rounds_env_zero(self, tmp_path, monkeypatch):
        """SECPT_CAMPAIGN_ROUNDS=0：不限轮，直到平台结束才停。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("SECPT_CAMPAIGN_ROUNDS", "0")
        calls: list[int] = []

        async def fake_run(spec, **kw):
            calls.append(1)
            return {
                "job_id": "x",
                "mode": "benchmark(parallel)",
                "flags": [],
                "chains": [],
                "open_remaining": 0,
                "wall_s": 0.1,
                "challenges_picked": 1,
                "ledger": {},
            }

        monkeypatch.setattr(bm, "run_benchmark", fake_run)
        rows = [
            [
                {
                    "unique_code": "a-1",
                    "flag_count": 1,
                    "correct_flag_count": 0,
                    "is_completed": False,
                }
            ],
            [
                {
                    "unique_code": "a-1",
                    "flag_count": 1,
                    "correct_flag_count": 0,
                    "is_completed": False,
                }
            ],
            [],
        ]
        fetch_n = [0]

        def fake_fetch():
            fetch_n[0] += 1
            # 消费方三个：轮前对账 + 轮后判定 + 轮入口快照。前 3 次=进行中，
            # 第 4 次起=平台结束（空列表）
            return rows[0] if fetch_n[0] <= 3 else rows[2]

        monkeypatch.setattr(bm, "fetch_challenges", fake_fetch)
        snap_calls = {"n": 0}

        def _snap(force=False, ttl=5):
            snap_calls["n"] += 1
            # 每轮间有两次实况查询（轮后判定 + 下轮开始）：第 1 次未完成→续跑，
            # 第 2 次未完成→进第 2 轮，第 3 次平台已结束 → break
            if snap_calls["n"] >= 3:
                raise bm.BenchmarkEnded("任务已结束")
            return {str(c["unique_code"]): c for c in bm.fetch_challenges()}

        monkeypatch.setattr(bm, "_platform_snapshot", _snap)
        monkeypatch.setattr(
            "secpt.core.orchestrator.ledger.ChallengeLedger.release_own_claims",
            lambda self, reason="": 0,
        )
        out = _run(bm.run_benchmark_campaign(_spec()))
        # 第 1 轮后仍有未完成题（不限轮，继续）；第 2 轮后平台结束 → 停
        assert len(calls) == 2
        assert out["campaign"]["rounds_done"] == 2

    def test_transient_snapshot_error_retries(self, tmp_path, monkeypatch):
        """轮间瞬时不可达（URLError→BenchmarkUnavailable）：退避重试后续跑，不提前收工。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("SECPT_CAMPAIGN_ROUNDS", "3")
        calls: list[int] = []

        async def fake_run(spec, **kw):
            calls.append(1)
            return {
                "job_id": "x",
                "mode": "benchmark(parallel)",
                "flags": [],
                "chains": [],
                "open_remaining": 0,
                "wall_s": 0.1,
                "challenges_picked": 1,
                "ledger": {},
            }

        async def _no_sleep(_s):
            return None

        monkeypatch.setattr(bm.asyncio, "sleep", _no_sleep)  # 退避不真睡（单测提速）
        monkeypatch.setattr(bm, "run_benchmark", fake_run)
        pending = [
            {
                "unique_code": "a-1",
                "flag_count": 1,
                "correct_flag_count": 0,
                "is_completed": False,
            }
        ]
        snaps = {"n": 0}

        def _snap(force=False, ttl=5):
            snaps["n"] += 1
            # #1 瞬时断网（helper 内部重试）→ #2 未完成 → 进第 2 轮
            # → #3 未完成（轮后判定）→ #4 平台已结束 → break
            if snaps["n"] == 1:
                raise bm.BenchmarkUnavailable("平台连接失败: timed out")
            if snaps["n"] >= 4:
                raise bm.BenchmarkEnded("任务已结束")
            return {str(c["unique_code"]): c for c in pending}

        monkeypatch.setattr(bm, "_platform_snapshot", _snap)
        monkeypatch.setattr(
            "secpt.core.orchestrator.ledger.ChallengeLedger.release_own_claims",
            lambda self, reason="": 0,
        )
        out = _run(bm.run_benchmark_campaign(_spec()))
        assert len(calls) == 2  # 断网没有把战役提前终止
        assert out["campaign"]["rounds_done"] == 2
        assert snaps["n"] >= 4  # 确实走过重试

    @pytest.mark.parametrize(
        ("cum", "expected"),
        [(None, "deepseek-v4-flash"), ("1", "glm/glm-5.3-flash")],
    )
    def test_restart_rotation_policy(self, tmp_path, monkeypatch, cum, expected):
        """默认按本段轮次（持久化历史不影响本段第 1 轮，仍用主脑）；
        SECPT_ROTATE_BY_CUMULATIVE=1 时才按累计轮次换脑。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("SECPT_CAMPAIGN_ROUNDS", "1")
        monkeypatch.setenv("SECPT_ESCALATE_MODEL", "glm/glm-5.3-flash")
        monkeypatch.delenv("BENCHMARK_TASK_ID", raising=False)
        monkeypatch.delenv("SECPT_ROTATE_BY_CUMULATIVE", raising=False)
        if cum:
            monkeypatch.setenv("SECPT_ROTATE_BY_CUMULATIVE", cum)
        models: list[str | None] = []

        async def fake_run(spec, **kw):
            models.append(kw.get("model"))
            return {
                "job_id": "x",
                "mode": "benchmark(parallel)",
                "flags": [],
                "chains": [],
                "open_remaining": 0,
                "wall_s": 0.1,
                "challenges_picked": 1,
                "ledger": {},
            }

        monkeypatch.setattr(bm, "run_benchmark", fake_run)
        pending = [
            {
                "unique_code": "a-1",
                "flag_count": 1,
                "correct_flag_count": 0,
                "is_completed": False,
            }
        ]
        monkeypatch.setattr(
            bm,
            "_platform_snapshot",
            lambda force=False, ttl=5: {str(c["unique_code"]): c for c in pending},
        )
        # 预置上一段进程已完成 1 轮：累计轮次=2，但本段轮次=1
        sp = bm.campaign_state_path(_spec())
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps({"rounds": [{"n": 1}]}), encoding="utf-8")
        out = _run(bm.run_benchmark_campaign(_spec(), model="deepseek-v4-flash"))
        assert models == [expected]
        assert out["campaign"]["rounds_done"] == 2

    def test_empty_snapshot_is_not_treated_as_complete(self, tmp_path, monkeypatch):
        """平台 200 空列表 ≠ 全部通关：退避重试后上抛，交给 entrypoint 重启续跑。"""
        import pytest

        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("SECPT_CAMPAIGN_ROUNDS", "0")
        calls: list[int] = []

        async def fake_run(spec, **kw):
            calls.append(1)
            return {
                "job_id": "x",
                "mode": "benchmark(parallel)",
                "flags": [],
                "chains": [],
                "open_remaining": 0,
                "wall_s": 0.1,
                "challenges_picked": 1,
                "ledger": {},
            }

        async def _no_sleep(_s):
            return None

        monkeypatch.setattr(bm.asyncio, "sleep", _no_sleep)
        monkeypatch.setattr(bm, "run_benchmark", fake_run)
        monkeypatch.setattr(bm, "_platform_snapshot", lambda force=False, ttl=5: {})
        with pytest.raises(bm.BenchmarkUnavailable):
            _run(bm.run_benchmark_campaign(_spec()))
        assert len(calls) == 1

    def test_rss_recycle_soft_landing_exit_3(self, tmp_path, monkeypatch):
        """RSS 触软限：本轮排干后以 SystemExit(3) 退出（entrypoint 重启续跑）。"""
        import time as _time

        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("SECPT_CAMPAIGN_ROUNDS", "0")
        monkeypatch.setenv("SECPT_RSS_SOFT_MB", "1")  # 1MB：第一次心跳即触发

        async def fake_run(spec, **kw):
            drain = kw.get("external_drain")
            assert drain is not None  # 软着陆信号必须传给本轮
            for _ in range(200):
                if drain.is_set():
                    break
                _time.sleep(0.05)
            assert drain.is_set()
            return {
                "job_id": "x",
                "mode": "benchmark(parallel)",
                "flags": [],
                "chains": [],
                "open_remaining": 0,
                "wall_s": 0.1,
                "challenges_picked": 1,
                "ledger": {},
            }

        monkeypatch.setattr(bm, "run_benchmark", fake_run)
        monkeypatch.setattr(bm, "fetch_challenges", lambda: [])

        def _ended(force=False, ttl=5):
            raise bm.BenchmarkEnded("任务已结束")

        monkeypatch.setattr(bm, "_platform_snapshot", _ended)
        monkeypatch.setattr(
            "secpt.core.orchestrator.ledger.ChallengeLedger.release_own_claims",
            lambda self, reason="": 0,
        )
        import pytest

        with pytest.raises(SystemExit) as ei:
            _run(bm.run_benchmark_campaign(_spec()))
        assert ei.value.code == 3


# ───────────────────────── A3：优雅排空 ─────────────────────────


class TestDrain:
    def test_should_drain_stops_claiming(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.delenv("SECPT_FRONTIER_DB", raising=False)
        called: list[dict] = []

        def fake_run_engine(**kw):
            called.append(kw)
            return SimpleNamespace(
                job_id="x", flag=None, steps=1, wall_s=0.01, status="done", messages=[]
            )

        monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
        fr = Frontier(frontier_db_path("dr1"))
        fr.add("待办", done_criteria="任意", max_steps=10)
        _run(
            run_frontier(
                "目标",
                profile="benchmark",
                base_job_id="dr1",
                frontier=fr,
                llm=object(),
                planner_llm=object(),
                max_intents=1,
                max_rounds=1,
                should_drain=lambda: True,
            )
        )
        assert called == []  # 排空状态下不认领任何意图


# ───────────────────────── B2/C3：playbook 蒸馏 ─────────────────────────


class TestPlaybook:
    def test_build_inject_and_experience(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        import secpt.learning.playbook as pb

        pb._BUILT = False  # 隔离全局懒建标记
        ddir = tmp_path / "knowledge" / "settle_digests"
        ddir.mkdir(parents=True)
        (ddir / "a.json").write_text(
            json.dumps(
                {
                    "title": "通关 e1-01（medium L1）",
                    "status": "done",
                    "steps": 21,
                    "tail": "WAF 绕过：分块传输 + HPP 命中 admin 接口，flag 在响应头",
                }
            ),
            encoding="utf-8",
        )
        (ddir / "b.json").write_text(
            json.dumps(
                {
                    "title": "通关 e1-02（medium L1）",
                    "status": "done",
                    "steps": 16,
                    "tail": "登录后越权：普通员工直接调审批接口成功",
                }
            ),
            encoding="utf-8",
        )
        builds = pb.build_playbooks()
        assert "e1" in builds and "WAF" in builds["e1"]
        section = pb.playbook_for("e1-09")
        assert "族级打法" in section and "WAF" in section and "自动蒸馏" in section
        assert pb.playbook_for("z-01") == ""  # 无该族数据 → 不注入
        assert pb.distill_experiences() >= 1  # C3：沉淀为经验
        assert pb.distill_experiences() == 0  # 幂等：已存在不重复写


# ───────────────────────── C1：进度锚点 ─────────────────────────


class TestProgressAnchor:
    def test_write_and_read(self, tmp_path):
        cfg = RunConfig(
            job_id="p1",
            objective="对目标做渗透并拿到 flag",
            system_extra="t",
            workspace=tmp_path,
            max_steps=10,
            max_wall_s=10,
        )
        write_progress_anchor(
            cfg,
            steps=7,
            session_round=1,
            actions=[(3, "bash", "curl target", "HTTP 200"), (7, "write_file", "p.py", "OK")],
            findings=2,
        )
        assert (tmp_path / "ws" / ".progress.md").is_file()  # 隐藏文件：不污染 ls 观测
        txt = progress_anchor_text(cfg)
        assert "压缩锚点" in txt and "step 7" in txt and "bash" in txt and "发现: 2" in txt


# ───────────────────────── C2：意图级工具 ─────────────────────────


class TestIntentTools:
    def test_tools_column_roundtrip(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        fr.add("web 题", tools="http_request,session", asset_scope="e1-01", max_steps=10)
        it = fr.claim_next()
        assert it is not None
        assert it.tools == "http_request,session"
        assert it.asset_scope == "e1-01"


class TestCampaignRoundModel:
    """轮间换脑：第 2+ 轮未完成题用升级模型重打（跨厂商第二脑视角）。"""

    def test_first_round_keeps_primary(self, monkeypatch):
        from secpt.core.orchestrator.benchmark import _campaign_round_model

        monkeypatch.setenv("SECPT_ESCALATE_MODEL", "glm/glm-4.6")
        assert _campaign_round_model("deepseek-v4-pro", 1) == "deepseek-v4-pro"

    def test_later_rounds_rotate_to_escalate(self, monkeypatch):
        from secpt.core.orchestrator.benchmark import _campaign_round_model

        monkeypatch.setenv("SECPT_ESCALATE_MODEL", "glm/glm-4.6")
        assert _campaign_round_model("deepseek-v4-pro", 2) == "glm/glm-4.6"
        assert _campaign_round_model("deepseek-v4-pro", 3) == "glm/glm-4.6"

    def test_rotate_off_keeps_primary(self, monkeypatch):
        from secpt.core.orchestrator.benchmark import _campaign_round_model

        monkeypatch.setenv("SECPT_ESCALATE_MODEL", "glm/glm-4.6")
        monkeypatch.setenv("SECPT_CAMPAIGN_ROTATE_MODEL", "0")
        assert _campaign_round_model("deepseek-v4-pro", 2) == "deepseek-v4-pro"

    def test_no_escalate_or_same_model_keeps_primary(self, monkeypatch):
        from secpt.core.orchestrator.benchmark import _campaign_round_model

        monkeypatch.delenv("SECPT_ESCALATE_MODEL", raising=False)
        assert _campaign_round_model("deepseek-v4-pro", 2) == "deepseek-v4-pro"
        assert _campaign_round_model("glm/glm-4.6", 2) == "glm/glm-4.6"  # 同名不空转
