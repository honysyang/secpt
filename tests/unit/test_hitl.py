"""D1+D2 测试：人在环路（steer/拦截审批/直投意图）+ frontier v2（依赖/并行/过程共享）。"""

from __future__ import annotations

import asyncio
import json
import threading
import time

from secpt.core.orchestrator.frontier import Frontier, run_frontier
from secpt.core.orchestrator.steer import SteerBox, SteerHook
from secpt.governance.intercept import BLOCK_TAG, Interceptor


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Steer
# ---------------------------------------------------------------------------


class TestSteerBox:
    def test_push_pull_consume(self, tmp_path):
        box = SteerBox(tmp_path / "s.db")
        assert box.pull("j1") is None
        box.push("j1", "去看 /api 的越权")
        assert box.pull("j1") == "去看 /api 的越权"
        assert box.pull("j1") is None  # 已消费
        box.push("j1", "a")
        box.push("j2", "b")  # 按 job 隔离
        assert box.pull("j1") == "a"
        assert box.pull("j2") == "b"


class TestSteerInjection:
    def test_hook_returns_message(self, tmp_path):
        consumed = []

        class Inner:
            def on_step(self, steps, elapsed):
                consumed.append(steps)

        box = SteerBox(tmp_path / "s.db")
        box.push("j1", "换方向")
        hook = SteerHook(Inner(), job_id="j1", box=box)
        msg = hook.on_step(1, 0.1)
        assert msg and "【人工操舵】" in msg and "换方向" in msg
        assert consumed == [1]  # 内层 hook 仍被调用
        assert hook.on_step(2, 0.2) is None  # 队列已空

    def test_engine_injects_steer(self, tmp_path, monkeypatch):
        """端到端：steer 消息在引擎下一步注入为 user 消息。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.core.job.fakellm import FakeLLM
        from secpt.runtime.engine import Engine, RunConfig

        box = SteerBox(tmp_path / "steer.sqlite")
        box.push("st1", "先停下来看看 /var/log 再决定")
        llm = FakeLLM(n_steps=3, job_id="st1", step_delay_s=0)  # 第 4 次调用 ANSWER

        class _Hooks:
            def __init__(self):
                from secpt.core.orchestrator.steer import SteerHook as SH

                self._h = SH(object(), job_id="st1", box=box)
                self.injected = 0

            def __getattr__(self, name):
                return getattr(self._h, name)

        cfg = RunConfig(
            job_id="st1",
            objective="o",
            system_extra="x",
            workspace=tmp_path,
            max_steps=6,
            max_wall_s=60,
        )
        engine = Engine(cfg, llm=llm, hooks=_Hooks())
        result = _run(engine.run())
        assert result.status == "success"
        # steer 注入发生了（user 消息里带操舵标记）
        steered = [
            m
            for m in result.messages
            if m.get("role") == "user" and "【人工操舵】" in str(m.get("content", ""))
        ]
        assert len(steered) >= 1


# ---------------------------------------------------------------------------
# 拦截审批
# ---------------------------------------------------------------------------


class TestIntercept:
    def test_seed_rules_loaded(self, tmp_path):
        it = Interceptor(tmp_path / "i.db")
        rules = it.list_rules()
        assert any(r["builtin"] for r in rules)
        assert any(r["action"] == "ask" for r in rules)

    def test_normal_command_passes(self, tmp_path):
        it = Interceptor(tmp_path / "i.db")
        ok, why = it.eval(tool="bash", command="curl -s http://t/", wait=False)
        assert ok and why == ""

    def test_deny_rule(self, tmp_path):
        it = Interceptor(tmp_path / "i.db")
        rid = it.add_rule(name="禁ping", pattern=r"\bping\s+10\.", action="deny", priority=50)
        ok, why = it.eval(tool="bash", command="ping 10.0.0.1", wait=False)
        assert not ok and BLOCK_TAG in why and "禁ping" in why
        it.remove_rule(rid)

    def test_ask_approve_flow(self, tmp_path):
        it = Interceptor(tmp_path / "i.db")

        # 后台线程在 0.3s 后审批
        def approver():
            time.sleep(0.3)
            rows = it.pending_list()
            assert rows, "pending 记录应已写入"
            it.decide(rows[0]["id"], True)

        t = threading.Thread(target=approver)
        t.start()
        ok, why = it.eval(tool="bash", command="rm -rf /tmp/whatever", job_id="j1")
        t.join()
        assert ok and why == ""

    def test_ask_deny_and_timeout(self, tmp_path):
        it = Interceptor(tmp_path / "i.db", seed=False)
        it.add_rule(name="全ask", pattern=".*", action="ask", timeout_s=1)
        ok, why = it.eval(tool="bash", command="ls", wait=False)
        assert not ok and "非阻塞模式拒绝" in why
        # 阻塞模式超时 → deny
        t0 = time.time()
        ok2, why2 = it.eval(tool="bash", command="ls")
        assert not ok2 and "超时" in why2 and time.time() - t0 >= 1.0

    def test_rule_lifecycle(self, tmp_path):
        it = Interceptor(tmp_path / "i.db")
        rid = it.add_rule(name="t", pattern="xxx", action="deny")
        assert it.set_enabled(rid, False) is True
        ok, _ = it.eval(tool="bash", command="xxx", wait=False)
        assert ok  # 停用后放行
        assert it.remove_rule(rid) is True
        assert it.remove_rule(1) is False  # 内置规则不可删


# ---------------------------------------------------------------------------
# 直投意图 + frontier v2
# ---------------------------------------------------------------------------


class TestDirectIntent:
    def test_has_open_direct(self, tmp_path):
        fr = Frontier(tmp_path / "f.db")
        assert not fr.has_open_direct()
        fr.add("planner 意图", source="planner")
        assert not fr.has_open_direct()
        iid = fr.add("人就测这个", source="direct", priority=9)
        assert fr.has_open_direct()
        fr.settle(iid, status="done")
        assert not fr.has_open_direct()

    def test_direct_mode_run(self, tmp_path, monkeypatch):
        """模式②：队列只有 direct 意图 → 执行它，planner 不产新意图。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        fr = Frontier(tmp_path / "fr.db")
        fr.add("人工指定的活儿", source="direct", priority=9, done_criteria="拿到结果")
        planner_calls = []

        class _LLM:
            async def call(self, messages, tools, model):
                planner_calls.append(1)
                return _resp("[]")

        from secpt.core.job.fakellm import FakeLLM as _Fake

        monkeypatch.delenv("SECPT_FAKE_LLM", raising=False)
        summary = _run(
            run_frontier(
                "目标",
                llm=_Fake(n_steps=2, job_id="d1", step_delay_s=0),
                planner_llm=_LLM(),
                frontier=fr,
                max_rounds=3,
                max_workers=1,
            )
        )
        # planner 未被调用（direct 暂停规划），direct 被执行
        assert planner_calls == []
        assert len(summary["chains"]) == 1
        assert summary["chains"][0]["source"] == "direct"

    def test_depends_on_unlock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        fr = Frontier(tmp_path / "fr.db")
        i1 = fr.add("第一步", source="direct")
        i2 = fr.add("第二步", source="direct", depends_on=i1)
        first = fr.claim_next()
        assert (first is not None and first.id == i2) or first.id == i1  # 认领按优先级
        # 依赖未满足 → deps_satisfied False
        second = fr.claim_next()
        if first.id == i2:
            assert not fr.deps_satisfied(_mk(first, depends_on=i1))
        else:
            assert not fr.deps_satisfied(_mk(second, depends_on=i1))
        fr.settle(i1, status="done")
        assert fr.deps_satisfied(_mk(second, depends_on=i1))


def _mk(row_or_intent, depends_on=""):
    from secpt.core.orchestrator.frontier import Intent

    if isinstance(row_or_intent, Intent):
        return row_or_intent
    return Intent(
        id=row_or_intent["id"],
        title=row_or_intent["title"],
        goal="",
        done_criteria="",
        priority=5,
        depth=0,
        status="open",
        attempts=0,
        depends_on=depends_on,
    )


class TestParallelClaim:
    def test_claim_is_atomic(self, tmp_path, monkeypatch):
        """并发认领不重复（max_workers=3 时同一条 intent 只被一个 worker 领走）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        fr = Frontier(tmp_path / "fr.db")
        ids = [fr.add(f"任务{i}", source="direct") for i in range(3)]
        claimed = []

        async def worker():
            intent = fr.claim_next()
            if intent:
                claimed.append(intent.id)

        async def _all():
            await asyncio.gather(*[worker() for _ in range(6)])

        _run(_all())
        assert sorted(claimed) == sorted(ids)  # 3 条各领一次，无重复


# ---------------------------------------------------------------------------
# 过程共享 + 指标
# ---------------------------------------------------------------------------


def _resp(content):
    class _Msg:
        def __init__(self, c):
            self.content = c

    class _R:
        def __init__(self, c):
            self.choices = [type("c", (), {"message": _Msg(c)})()]

    return _R(content)


class TestTracesAndMetrics:
    def test_search_traces_cross_job(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.core.job.store import JobStore

        store = JobStore(str(tmp_path / "jobs.sqlite"))
        store.upsert_job(
            __import__("secpt.core.job.models", fromlist=["JobRecord"]).JobRecord(
                job_id="ja",
                spec=__import__("secpt.core.job.models", fromlist=["TaskSpec"]).TaskSpec(
                    job_id="ja", objective="o"
                ),
            )
        )
        store.add_event("ja", "tool_result", {"preview": "响应里出现 SAMLResponse 签名异常"})
        store.add_event("ja", "assistant", {"content": "先放下这个方向"})

        hits = store.search_traces("SAMLResponse", exclude_job="jb")
        assert hits and hits[0]["job_id"] == "ja"
        assert "签名异常" in hits[0]["preview"]
        assert store.search_traces("SAMLResponse", exclude_job="ja") == []

    def test_metrics_smoke(self, tmp_path, monkeypatch):
        from secpt.cli.cmd_ops import metrics_cmd

        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.findings.store import FindingStore

        fs = FindingStore(tmp_path / "f.db")
        f = fs.save(
            __import__("secpt.findings.models", fromlist=["Finding"]).Finding(
                title="XSS", target="t"
            )
        )
        fs.record_reproduction(f.id, job_id="v1", verdict="reproduced")

        data = None
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            metrics_cmd(fmt="json", _finding_store=fs)
        data = json.loads(buf.getvalue())
        assert data["findings"]["confirmed"] == 1
        assert data["findings"]["retest_reproduced"] == 1
