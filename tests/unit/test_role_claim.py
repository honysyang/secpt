"""阶段二：团队角色认领/去重/资产挂靠的验收测试。

覆盖验收判据：
- claim_next(role=...) 角色过滤：exploit worker 不抢 recon 意图（认领互斥）
- find_duplicate：同 role+asset_scope 已有未结算意图 → 去重（防多轮规划重复派资产）
- plan_round 资产挂靠强制：声明角色但 asset_scope 不在【已有资产实体】→ 丢弃不派发
- build_intent_objective：角色意图追加角色化阶段提示（对齐 specialist objective_extra）
- 崩溃恢复：被杀进程遗留的 running 意图在 Frontier 重建时置回 open
"""

from __future__ import annotations

import asyncio
import json

from secpt.core.orchestrator.frontier import (
    Frontier,
    build_intent_objective,
    plan_round,
)
from secpt.core.orchestrator.roles import ROLE_MAP, role_stage
from secpt.intel.store import IntelStore


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _json_llm(bodies: list[str]):
    """planner 假 LLM：按轮次返回 JSON 字符串。"""

    class _L:
        def __init__(self):
            self._rounds = list(bodies)
            self.calls = 0

        async def call(self, messages, tools, model):
            self.calls += 1
            body = self._rounds.pop(0) if self._rounds else "[]"

            class _Msg:
                pass

            class _Resp:
                pass

            m = _Msg()
            m.content = body
            r = _Resp()
            r.choices = [type("c", (), {"message": m})()]
            return r

    return _L()


class TestRoleClaim:
    def test_claim_role_filter(self, tmp_path):
        """角色过滤：recon worker 只领 recon（或未定角色）；不抢 exploit。"""
        fr = Frontier(tmp_path / "f.db")
        recon = fr.add("资产侦察（recon）", role="recon", asset_scope="http://t:1")
        expl = fr.add("利用 http://t:1", role="exploit", asset_scope="http://t:1")

        # exploit worker：recon 意图不可见（被过滤），但未定角色的老意图可见
        i = fr.claim_next(role="exploit")
        assert i is not None and i.id == expl
        # 加强①资产租约：expl 在途时同资产（http://t:1）的 recon 不得并发进入
        assert fr.lease_holder("http://t:1") == expl
        assert fr.claim_next(role="recon") is None
        # 结算释放租约后，recon worker 才能领同资产的 recon 意图
        fr.settle(expl, status="done", result="exploit 完成")
        i2 = fr.claim_next(role="recon")
        assert i2 is not None and i2.id == recon
        # 队列空（recon 在途持有租约，exploit 侧同样领不到）
        assert fr.claim_next(role="exploit") is None

    def test_claim_role_filter_sees_plain(self, tmp_path):
        """未定角色（role=''）意图对任意角色 worker 可见（老意图兼容）。"""
        fr = Frontier(tmp_path / "f.db")
        plain = fr.add("普通意图")
        i = fr.claim_next(role="exploit")
        assert i is not None and i.id == plain

    def test_find_duplicate(self, tmp_path):
        """同 role+asset_scope 未结算 → True；结算后 → False；不同键 → False。"""
        fr = Frontier(tmp_path / "f.db")
        assert fr.find_duplicate("exploit", "http://t:1") is False
        iid = fr.add("利用 http://t:1", role="exploit", asset_scope="http://t:1")
        assert fr.find_duplicate("exploit", "http://t:1") is True
        assert fr.find_duplicate("recon", "http://t:1") is False  # 不同角色不算重
        assert fr.find_duplicate("exploit", "http://t:2") is False  # 不同资产不算重
        fr.settle(iid, status="done")
        assert fr.find_duplicate("exploit", "http://t:1") is False  # 已结算可再派
        # 空 role / 空 asset 不参与去重（普通意图不受影响）
        assert fr.find_duplicate("", "http://t:1") is False

    def test_plan_round_drops_unbound_role_intents(self, tmp_path, monkeypatch):
        """资产挂靠强制：声明角色的意图 asset_scope 不在已有实体集 → 丢弃不派发。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        # 种子资产：仅 http://seed:8000 在实体集内
        st = IntelStore(tmp_path / "intel.sqlite")
        st.observe_asset("endpoint", "http://seed:8000", job_id="test-seed")

        body = json.dumps(
            [
                {
                    "title": "打种子资产",
                    "goal": "拿 flag",
                    "done_criteria": "ANSWER flag",
                    "priority": 8,
                    "depth": 1,
                    "role": "exploit",
                    "asset_scope": "http://seed:8000",  # 在实体集 → 派发
                },
                {
                    "title": "打空想资产",
                    "goal": "拿 flag",
                    "done_criteria": "ANSWER flag",
                    "priority": 9,
                    "depth": 1,
                    "role": "exploit",
                    "asset_scope": "http://phantom:9999",  # 不在实体集 → 丢弃
                },
            ],
            ensure_ascii=False,
        )
        fr = Frontier(tmp_path / "fr.db")
        n = _run(plan_round("目标：打它", fr, _json_llm([body]), model="m", max_new=3))
        assert n == 1  # 只有挂靠成功资产的意图被派发
        rows = fr.list_all()
        assert len(rows) == 1
        assert rows[0]["asset_scope"] == "http://seed:8000"
        assert rows[0]["role"] == "exploit"

    def test_plan_round_dedup(self, tmp_path, monkeypatch):
        """去重：同 asset_scope+role 已存在（open）→ 不再派（防多轮重复派同一资产）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        st = IntelStore(tmp_path / "intel.sqlite")
        st.observe_asset("endpoint", "http://seed:8000", job_id="test-seed")

        fr = Frontier(tmp_path / "fr.db")
        body = json.dumps(
            [
                {
                    "title": "利用 http://seed:8000",
                    "goal": "拿 flag",
                    "done_criteria": "ANSWER flag",
                    "priority": 8,
                    "depth": 1,
                    "role": "exploit",
                    "asset_scope": "http://seed:8000",
                }
            ],
            ensure_ascii=False,
        )
        n1 = _run(plan_round("目标", fr, _json_llm([body]), model="m"))
        assert n1 == 1
        # 第二轮同资产同角色 → 去重，不再派
        n2 = _run(plan_round("目标", fr, _json_llm([body]), model="m"))
        assert n2 == 0
        assert len(fr.list_all()) == 1


class TestRoleStage:
    def test_role_map_complete(self):
        """四角色闭环齐备：recon/exploit/verify/report。"""
        assert set(ROLE_MAP) == {"recon", "exploit", "verify", "report"}

    def test_build_intent_objective_injects_role_stage(self, tmp_path):
        """角色意图的 objective 追加角色化阶段提示；普通意图不追加。"""
        fr = Frontier(tmp_path / "f.db")
        fr.add("利用 http://t:1", role="exploit", asset_scope="http://t:1")
        fr.add("普通意图")

        # 同优先级同深度 → 先创建先领（role 意图先入队）
        ri = fr.claim_next()
        assert ri is not None and ri.role == "exploit"
        obj = build_intent_objective("总目标", ri, fr.recent_results())
        assert "【当前阶段：定向利用（exploit）】" in obj
        assert "http://t:1" in obj  # 角色 stage 带上挂靠资产
        fr.settle(ri.id, status="done", result="ok")

        # 普通意图不注入角色 stage
        pi = fr.claim_next()
        assert pi is not None and pi.role == ""
        obj_plain = build_intent_objective("总目标", pi, fr.recent_results())
        assert "【当前阶段：" not in obj_plain

    def test_role_stage_unknown_role(self):
        """未知角色 → 空 stage（不破坏普通意图渲染）。"""
        assert role_stage("unknown") == ""
        assert role_stage("") == ""


class TestCrashRecovery:
    def test_stale_running_reopens_on_init(self, tmp_path):
        """崩溃恢复：遗留 running 意图在 Frontier 重建时置回 open（可重新认领）。"""
        fr1 = Frontier(tmp_path / "f.db")
        # 模拟被杀进程：意图卡在 running
        fr1.add("半途而废的意图")
        fr1.claim_next()
        assert fr1.list_all()[0]["status"] == "running"

        # 重建 Frontier（新编排上下文）→ running 恢复为 open
        fr2 = Frontier(tmp_path / "f.db")
        rec = fr2.list_all()[0]
        assert rec["status"] == "open"
        assert fr2.claim_next() is not None  # 可重新认领

    def test_team_preset_sets_role_and_deps(self, tmp_path):
        """团队模式意图骨架：recon 先行 + exploit 依赖 recon（见 run_frontier 集成）。"""
        fr = Frontier(tmp_path / "f.db")
        recon = fr.add("资产侦察（recon）", role="recon", priority=9)
        expl = fr.add(
            "利用 http://t:1",
            role="exploit",
            asset_scope="http://t:1",
            priority=7,
            depends_on=recon,
        )
        assert expl != recon
        # exploit 依赖未满足前领到会 blocked；recon done 后解锁
        i = fr.claim_next()
        assert i is not None and i.role == "recon"  # 高优先先行
        i2 = fr.claim_next()
        assert i2 is not None and i2.role == "exploit"
        assert fr.deps_satisfied(i2) is False
        # 放回 exploit（模拟依赖未满足时的 blocked+refund），recon 结算后解锁
        fr.settle(expl, status="open")
        fr.settle(recon, status="done")
        i3 = fr.claim_next()
        assert i3 is not None and fr.deps_satisfied(i3) is True
