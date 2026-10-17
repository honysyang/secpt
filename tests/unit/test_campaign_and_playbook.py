"""A2/A3/B1/B2/C1/C2 测试：战役多轮、排空、族级 playbook、进度锚点、意图级工具。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

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
        seq = iter(rows)
        monkeypatch.setattr(bm, "fetch_challenges", lambda: next(seq))
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
