"""B4 planner/frontier 测试：队列语义 / 深度优先 / 主循环收敛。"""

from __future__ import annotations

import asyncio
import json
from typing import ClassVar

from secpt.core.orchestrator.frontier import (
    Frontier,
    build_intent_objective,
    frontier_db_path,
    plan_round,
    run_frontier,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestFrontier:
    def test_add_claim_settle(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        fr.add("打入口A", priority=5, depth=0)
        i2 = fr.add("深入A1", priority=5, depth=1)
        i3 = fr.add("打入口B", priority=8, depth=0)
        # 认领顺序：priority DESC → depth DESC → 先创建先领
        first = fr.claim_next()
        assert first is not None and first.id == i3  # priority 8 最先
        second = fr.claim_next()
        assert second is not None and second.id == i2  # 同 priority，depth 1 优先（深度优先）
        assert second.attempts == 1
        fr.settle(second.id, status="done", result="拿到 shell")
        assert fr.count_open() == 1  # i1 仍 open
        res = fr.recent_results()
        assert res and res[0]["result"] == "拿到 shell"

    def test_claim_empty(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        assert fr.claim_next() is None

    def test_deps_unsatisfied_goes_blocked(self, tmp_path):
        """依赖未满足：意图置 blocked 而非 open，不空耗 attempts。"""
        fr = Frontier(tmp_path / "f.db")
        a = fr.add("找注入点", priority=5)
        b = fr.add("拿凭据", priority=8, depends_on=a)  # 高优先但依赖未满足
        i = fr.claim_next()
        assert i.id == b  # 高优先先被领
        # 与 _worker_loop 阻塞分支同序列：settle(blocked) 后回退 claim 消耗
        fr.settle(i.id, status="blocked", fail_reason="依赖未满足")
        fr.refund_attempt(i.id)
        assert fr.count_open() == 1  # 只剩 a
        assert fr.claim_next().id == a
        # blocked 意图不再被反复 claim（未真正执行的 claim 已回退，不虚增 attempts）
        assert fr.claim_next() is None
        rec = fr.list_all(status="blocked")
        assert len(rec) == 1 and rec[0]["attempts"] == 0

    def test_deps_unlock_reopens(self, tmp_path):
        """依赖解锁：前置意图 done 后 blocked 意图恢复可领。"""
        fr = Frontier(tmp_path / "f.db")
        a = fr.add("拿注入点", priority=5)
        b = fr.add("拿凭据", priority=8, depends_on=a)
        i = fr.claim_next()
        assert i.id == b
        fr.settle(b, status="blocked", fail_reason="依赖未满足")
        fr.refund_attempt(b)
        assert fr.reopen_ready() == 0  # 依赖未满足，不解锁
        fr.settle(a, status="done")
        assert fr.reopen_ready() == 1  # 解锁
        nxt = fr.claim_next()
        assert nxt.id == b and nxt.attempts == 1

    def test_subgoal_roundtrip(self, tmp_path):
        """Goals 层：add 带 subgoal 持久化（含老库迁移列）。"""
        fr = Frontier(tmp_path / "f.db")
        fr.add("扫登录接口", subgoal="找注入点")
        rows = fr.list_all()
        assert rows[0]["subgoal"] == "找注入点"

    def test_subgoal_coverage_and_settled(self, tmp_path):
        """覆盖度聚合 + 收敛判定：全子目标结算后才 True。"""
        fr = Frontier(tmp_path / "f.db")
        a = fr.add("扫登录接口", subgoal="找注入点")
        b = fr.add("打凭据接口", subgoal="拿凭据")
        cov = fr.subgoal_coverage()
        assert len(cov) == 2 and all(x["pending"] == 1 for x in cov)
        assert fr.all_subgoals_settled() is False
        fr.settle(a, status="done")
        assert fr.all_subgoals_settled() is False  # 拿凭据仍 pending
        fr.settle(b, status="exhausted", fail_reason="没洞")
        assert fr.all_subgoals_settled() is True
        by = {x["subgoal"]: x for x in fr.subgoal_coverage()}
        assert by["找注入点"]["done"] == 1 and by["拿凭据"]["exhausted"] == 1

    def test_no_subgoals_not_settled(self, tmp_path):
        """无 subgoal 时不启用覆盖度收敛（退回 idle_rounds，向后兼容）。"""
        fr = Frontier(tmp_path / "f.db")
        fr.add("普通意图")
        assert fr.all_subgoals_settled() is False

    def test_plan_round_records_subgoal(self, tmp_path, monkeypatch):
        """plan_round：解析 subgoal 字段 → intents 落库 + 探索图登记 goal 节点。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        fr = Frontier(tmp_path / "f.db")
        body = json.dumps(
            [
                {
                    "title": "扫登录接口",
                    "goal": "摸指纹",
                    "done_criteria": "有指纹",
                    "priority": 5,
                    "depth": 0,
                    "subgoal": "找注入点",
                }
            ],
            ensure_ascii=False,
        )
        planner = _PlannerLLM([body])
        n = _run(plan_round("目标：拿下 test", fr, planner, model="m"))
        assert n == 1
        assert fr.list_all()[0]["subgoal"] == "找注入点"
        from secpt.intel.store import IntelStore

        st = IntelStore(tmp_path / "intel.sqlite")
        with st._conn() as c:
            n_goals = c.execute("SELECT COUNT(*) n FROM nodes WHERE type='goal'").fetchone()["n"]
        assert n_goals == 1


class _PlannerLLM:
    """第一轮产 2 意图，之后产空（收敛）。"""

    def __init__(self, rounds_json: list[str]):
        self._rounds = list(rounds_json)
        self.calls = 0

    async def call(self, messages, tools, model):
        self.calls += 1

        class _Msg:
            def __init__(self, c):
                self.content = c

        class _Resp:
            def __init__(self, c):
                self.choices = [type("c", (), {"message": _Msg(c)})()]

        body = self._rounds.pop(0) if self._rounds else "[]"
        return _Resp(body)


class _WorkerLLM:
    """每条 intent：1 次 bash 后按意图标题决定是否 ANSWER。"""

    async def call(self, messages, tools, model):
        system = messages[0]["content"]
        answered = getattr(self, "_n", 0)
        self._n = answered + 1
        if self._n % 2 == 0 or "admin" in system:
            return _answer_resp("FLAG{pwned}")  # 真实 flag 形态（P0-1 成果语义）
        return _tool_resp(self._n)


def _tool_resp(i):
    class _Fn:
        name, arguments = "bash", json.dumps({"command": "ls"})

    class _TC:
        def __init__(self):
            self.id = f"tc{i}"
            self.function = _Fn()

    class _Msg:
        content = f"step {i}"
        tool_calls: ClassVar[list] = [_TC()]
        reasoning_content = None

    class _Resp:
        choices: ClassVar[list] = [type("c", (), {"message": _Msg()})()]

    return _Resp()


def _answer_resp(flag):
    class _Msg:
        content = f"ANSWER: {flag}"
        tool_calls: ClassVar[list] = []
        reasoning_content = None

    class _Resp:
        choices: ClassVar[list] = [type("c", (), {"message": _Msg()})()]

    return _Resp()


class TestRunFrontier:
    def test_full_loop_converges(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        plan_json = json.dumps(
            [
                {
                    "title": "侦察目标",
                    "goal": "摸清指纹",
                    "done_criteria": "有指纹",
                    "priority": 5,
                    "depth": 0,
                },
                {
                    "title": "打admin后台",
                    "goal": "拿flag",
                    "done_criteria": "ANSWER flag",
                    "priority": 9,
                    "depth": 1,
                },
            ],
            ensure_ascii=False,
        )
        planner = _PlannerLLM([plan_json])
        summary = _run(
            run_frontier(
                "目标：拿下 test",
                llm=_WorkerLLM(),
                planner_llm=planner,
                base_job_id="ht1",
                max_rounds=6,
                frontier=Frontier(tmp_path / "fr.db"),
            )
        )
        assert summary["mode"] == "frontier"
        # admin intent priority 9 先执行并拿到 flag → 整体收敛
        assert summary["flags"] == ["FLAG{pwned}"]  # P0-1：只采信真实 flag 形态
        statuses = {c["intent"]: c["status"] for c in summary["chains"]}
        assert statuses.get("打admin后台") == "done"
        # P0-2：父 job 事件流已聚合（watch 导出 / 评判的数据源）
        assert summary.get("sub_jobs")  # 有子任务
        parent_events = tmp_path / "jobs" / "ht1" / "events.jsonl"
        assert parent_events.is_file()
        lines = [json.loads(x) for x in parent_events.read_text().splitlines() if x.strip()]
        assert all(ev["job_id"] == "ht1" for ev in lines)  # job_id 统一为父 job
        assert any((ev.get("payload") or {}).get("sub_jobs") for ev in lines)  # 收尾 summary

    def test_intent_objective_shape(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        fr.add("测试某接口", goal="拿到敏感数据", done_criteria="响应含 secret")
        intent = fr.claim_next()
        assert intent is not None
        obj = build_intent_objective("总目标", intent, fr.recent_results())
        assert "当前意图" in obj and "完成判据" in obj and "总目标" in obj


class TestProgressAwareResume:
    """⑦ 进展感知续步判据：ws 有实质产出（除 api.py 模板）才续预算。"""

    def test_ws_output_detection(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.core.orchestrator.frontier import _has_ws_output

        assert _has_ws_output("") is False  # 无 job_id
        assert _has_ws_output("j-none") is False  # 无 job 目录
        ws = tmp_path / "jobs" / "j1" / "ws"
        ws.mkdir(parents=True)
        (ws / "api.py").write_text("print(1)")
        assert _has_ws_output("j1") is False  # 仅模板 → 不算进展（空转不续）
        (ws / "exploit.py").write_text("payload")
        assert _has_ws_output("j1") is True  # 实质产出 → 攻击路径成型，可续


class TestIncrementalAggregation:
    """④ 事件增量聚合：settle 即并入父事件流，重复聚合幂等。"""

    def test_incremental_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.core.orchestrator.frontier import _aggregate_sub_events

        sub = tmp_path / "jobs" / "base-i1"
        sub.mkdir(parents=True)
        (sub / "events.jsonl").write_text(
            json.dumps({"seq": 1, "type": "round_start", "payload": {"n": 1}}) + "\n",
            encoding="utf-8",
        )
        agg: set[str] = set()
        chains = [{"intent": "x", "status": "done"}]
        _aggregate_sub_events("base", ["base-i1"], chains, ["F{}"], aggregated=agg, final=False)
        parent = tmp_path / "jobs" / "base" / "events.jsonl"
        n1 = len(parent.read_text().splitlines())
        assert n1 == 1 and agg == {"base-i1"}
        # 重复增量（同一子 job 已并入）→ 跳过，不重复写
        _aggregate_sub_events("base", ["base-i1"], chains, ["F{}"], aggregated=agg, final=False)
        assert len(parent.read_text().splitlines()) == n1
        # 收尾（final=True）：无新子 job 时补 summary 事件
        _aggregate_sub_events("base", ["base-i1"], chains, ["F{}"], aggregated=agg, final=True)
        lines = parent.read_text().splitlines()
        assert len(lines) == 2 and json.loads(lines[-1])["payload"]["chains"] == 1


class TestGraphAnchor:
    """⑧ 双图锚点：意图落探索图并挂靠资产 → 资产可反查测过的路径。"""

    def test_intent_recorded_and_reverse_lookup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.intel.store import IntelStore

        fr = Frontier(tmp_path / "f.db")
        fr.add("打后台越权", role="exploit", asset_scope="http://t/admin")
        st = IntelStore(tmp_path / "intel.sqlite")
        rows = st.nodes_for_asset("endpoint", "http://t/admin")
        assert any(r["type"] == "intent" and "打后台越权" in r["content"] for r in rows)


class TestTaskScopedDb:
    """F1 回归：意图库按任务隔离（此前共享 var/frontier.sqlite）。"""

    def test_db_path_under_job_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.delenv("SECPT_FRONTIER_DB", raising=False)
        assert frontier_db_path("abc123") == tmp_path / "jobs" / "abc123" / "frontier.sqlite"

    def test_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_FRONTIER_DB", str(tmp_path / "custom.sqlite"))
        assert frontier_db_path("x") == tmp_path / "custom.sqlite"

    def test_tasks_isolated(self, tmp_path, monkeypatch):
        """两个任务的意图互不可见、互不认领（F1 前共享库时会互抢）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.delenv("SECPT_FRONTIER_DB", raising=False)
        a = Frontier(frontier_db_path("task-a"))
        b = Frontier(frontier_db_path("task-b"))
        a.add("A 的意图", source="planner")
        assert b.claim_next() is None  # B 领不到 A 的单
        assert b.count_open() == 0 and b.total() == 0  # B 的配额零污染
        first = a.claim_next()
        assert first is not None and first.title == "A 的意图"

    def test_recover_stale_does_not_cross_tasks(self, tmp_path, monkeypatch):
        """F1 核心回归：新任务是运行中任务的另一 DB——_recover_stale 不再
        把对方的 running 意图置回 open（此前共享库下会被第二实例双执行）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.delenv("SECPT_FRONTIER_DB", raising=False)
        a = Frontier(frontier_db_path("task-a"))
        a.add("在途意图")
        claimed = a.claim_next()
        assert claimed is not None and claimed.status == "running"
        Frontier(frontier_db_path("task-b"))  # B 启动（会跑 _recover_stale）
        rows = a.list_all(status="running")
        assert len(rows) == 1, "B 的启动不得把 A 的在途意图置回 open"

    def test_run_frontier_default_uses_task_db(self, tmp_path, monkeypatch):
        """run_frontier 缺省库必须落在 jobs/<base>/ 下（不再共享 var 根）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.delenv("SECPT_FRONTIER_DB", raising=False)
        llm = _PlannerLLM(["[]"])  # 产空意图 → 快速收敛
        _run(
            run_frontier(
                "测试目标",
                profile="ctf",
                base_job_id="scopechk",
                llm=llm,
                planner_llm=llm,
                max_rounds=1,
            )
        )
        assert (tmp_path / "jobs" / "scopechk" / "frontier.sqlite").is_file()


class TestAssetLease:
    """加强①：资产级在途互斥（同资产意图不并发；settle/崩溃释放）。"""

    def test_same_asset_claim_excluded(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        a = fr.add("A", role="exploit", asset_scope="http://t:1", priority=9, depth=0)
        b = fr.add("B", role="exploit", asset_scope="http://t:1", priority=5, depth=0)
        c = fr.add("C 无资产", depth=0)
        first = fr.claim_next()
        assert first.id == a
        assert fr.lease_holder("http://t:1") == a
        # 同资产 B 被租约挡住；无资产意图照常可领
        assert fr.claim_next().id == c
        assert fr.claim_next() is None
        # 结算释放租约 → B 恢复可领
        fr.settle(a, status="done")
        assert fr.lease_holder("http://t:1") is None
        assert fr.claim_next().id == b

    def test_blocked_refund_releases_lease(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        fr.add("A", role="exploit", asset_scope="http://t:2", depth=0)
        it = fr.claim_next()
        assert fr.lease_holder("http://t:2") == it.id
        # 依赖未满足回退路径：settle(blocked)+refund 也必须释放租约
        fr.settle(it.id, status="blocked", fail_reason="依赖未满足")
        fr.refund_attempt(it.id)
        assert fr.lease_holder("http://t:2") is None

    def test_recover_stale_clears_lease(self, tmp_path):
        """进程被杀：新实例把 running 置回 open 并回收其租约（防幽灵锁死资产）。"""
        db = tmp_path / "f.db"
        fr = Frontier(db)
        fr.add("A", role="exploit", asset_scope="http://t:3", depth=0)
        fr.claim_next()
        assert fr.lease_holder("http://t:3") is not None
        fr2 = Frontier(db)  # 模拟重启
        assert fr2.lease_holder("http://t:3") is None
        assert fr2.claim_next() is not None

    def test_completed_asset_skips_second_intent(self, tmp_path, monkeypatch):
        """本次运行内同资产已完成 → 第二个意图跳过执行直接结算 done。"""
        from types import SimpleNamespace

        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.delenv("SECPT_FRONTIER_DB", raising=False)
        calls: list[str] = []

        def fake_run_engine(**kw):
            calls.append(str(kw.get("job_id")))
            return SimpleNamespace(
                job_id=kw.get("job_id"),
                flag="FLAG{lease_x}",
                steps=3,
                wall_s=0.01,
                status="success",
                messages=[],
            )

        monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
        fr = Frontier(frontier_db_path("sk1"))
        fr.add(
            "利用 A",
            role="exploit",
            asset_scope="http://10.9.9.9:80",
            priority=9,
            depth=0,
            done_criteria="拿到 flag",
        )
        fr.add(
            "利用 A 变体",
            role="exploit",
            asset_scope="http://10.9.9.9:80",
            priority=5,
            depth=0,
            done_criteria="拿到 flag",
        )
        summary = _run(
            run_frontier(
                "目标",
                profile="ctf",
                base_job_id="sk1",
                frontier=fr,
                llm=object(),
                planner_llm=_PlannerLLM(["[]"]),
                max_rounds=3,
                stop_on_first_flag=False,
            )
        )
        assert len(calls) == 1  # 第二个同资产意图未执行引擎
        steps = [c["steps"] for c in summary["chains"]]
        assert steps == [3, 0]


class TestIntentEdit:
    """加强③：意图编辑/撤回（人机通道）。"""

    def test_update_priority_effective_and_running_refused(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        a = fr.add("低", depth=0, priority=1)
        fr.add("高", depth=0, priority=9)
        assert fr.update(a, priority=10) is True
        assert fr.claim_next().id == a  # 改优先级立即生效
        assert fr.update(a, priority=1) is False  # running 拒绝改
        assert fr.update("nope", priority=1) is False

    def test_remove_only_open_blocked(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        a = fr.add("可撤", depth=0)
        b = fr.add("在途", depth=0, priority=9)
        assert fr.remove(a) is True
        claimed = fr.claim_next()
        assert claimed.id == b
        assert fr.remove(b) is False  # running 拒绝删
        assert fr.remove("nope") is False


class TestTaskLock:
    """加强④：任务级单编排锁。"""

    def test_second_acquire_rejected_then_released(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.core.orchestrator.frontier import _acquire_task_lock, _release_task_lock

        h = _acquire_task_lock("lk1")
        assert h is not None
        try:
            _acquire_task_lock("lk1")
            raise AssertionError("第二个实例必须被拒绝")
        except RuntimeError as e:
            assert "lk1" in str(e)
        _release_task_lock(h)
        h2 = _acquire_task_lock("lk1")  # 释放后可再取
        assert h2 is not None
        _release_task_lock(h2)


class TestRecheckOnce:
    """加强②：复核纪律运行时强制（首轮失败自动回队一次）。"""

    def test_failed_intent_rechecked_once(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.delenv("SECPT_FRONTIER_DB", raising=False)
        calls: list[str] = []

        def fake_run_engine(**kw):
            calls.append(str(kw.get("job_id")))
            return SimpleNamespace(
                job_id=kw.get("job_id"),
                flag=None,
                steps=2,
                wall_s=0.01,
                status="step_exceeded",
                messages=[],
            )

        monkeypatch.setattr("secpt.cli.engine_runner.run_engine", fake_run_engine)
        fr = Frontier(frontier_db_path("rk1"))
        fr.add("顽固意图", done_criteria="拿到 flag", depth=0)
        _run(
            run_frontier(
                "目标",
                profile="ctf",
                base_job_id="rk1",
                frontier=fr,
                llm=object(),
                planner_llm=_PlannerLLM(["[]"]),
                max_rounds=3,
            )
        )
        assert len(calls) == 2  # 首跑 + 自动复核一次
        rows = fr.list_all(status="exhausted")
        assert len(rows) == 1 and rows[0]["attempts"] == 2  # 第二次失败才终态


class TestReconIngest:
    """加强⑤：recon 资产清单确定性入 intel 图。"""

    def test_ingest_recon_assets(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.core.orchestrator.frontier import _ingest_recon_assets
        from secpt.intel.store import IntelStore

        p = tmp_path / "assets.json"
        p.write_text(
            json.dumps([{"host": "a.example.com", "port": 443, "service": "https"}]),
            encoding="utf-8",
        )
        assert _ingest_recon_assets(p, job_id="t1") == 1
        st = IntelStore(tmp_path / "intel.sqlite")
        vals = [str(a["value"]) for a in st.assets(limit=10)]
        assert any("a.example.com" in v for v in vals)
