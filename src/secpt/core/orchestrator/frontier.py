"""planner/frontier：意图驱动的深度优先调度（多步利用链不丢计划）。

CHECKMATE/HPTSA 的可迁移最小实现：
- planner 每轮全新会话（省 context、防漂移），只负责产意图（intent）入 frontier
- frontier 存 SQLite——常驻 todolist 跨 resume/重启持久化（比 ARTEX 内存版强，天然配 7×24）
- worker = 现有单链 Engine：领一条 intent 跑到自宣告，计划本身不依赖模型上下文记忆
- 深度优先 + 否定记账：intent 有 depth 与 attempts，失败两次即 exhausted（至多复核一次）
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("SECPT_VAR", "var")) / "frontier.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT DEFAULT '',
    done_criteria TEXT DEFAULT '',
    priority INTEGER DEFAULT 5,
    depth INTEGER DEFAULT 0,
    status TEXT DEFAULT 'open',        -- open/running/done/exhausted/blocked
    source TEXT DEFAULT 'planner',     -- planner | direct（人工直投：规划者见 direct 即暂停）
    depends_on TEXT DEFAULT '',        -- 逗号分隔 intent_id 或 fact:谓词（D2 依赖解锁）
    max_steps INTEGER DEFAULT 0,       -- 意图级步数预算（0=不限；>0 硬止损，收益递减机械化）
    subgoal TEXT DEFAULT '',            -- Goals 层：本意图服务的子目标（跨轮方向一致性的锚点）
    job_id TEXT DEFAULT '',
    result TEXT DEFAULT '',
    attempts INTEGER DEFAULT 0,
    fail_reason TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intents_status ON intents(status, priority DESC, depth);
"""


@dataclass
class Intent:
    id: str
    title: str
    goal: str
    done_criteria: str
    priority: int
    depth: int
    status: str
    attempts: int
    source: str = "planner"
    depends_on: str = ""
    max_steps: int = 0


class Frontier:
    """意图队列（SQLite 持久化 todolist）。"""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            self._migrate(c)

    def _migrate(self, c: sqlite3.Connection) -> None:
        """老库补列（CREATE IF NOT EXISTS 不会给已存在的表加新列）。"""
        cols = {r["name"] for r in c.execute("PRAGMA table_info(intents)")}
        if "source" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN source TEXT DEFAULT 'planner'")
        if "depends_on" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN depends_on TEXT DEFAULT ''")
        if "max_steps" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN max_steps INTEGER DEFAULT 0")
        if "subgoal" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN subgoal TEXT DEFAULT ''")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def add(
        self,
        title: str,
        *,
        goal: str = "",
        done_criteria: str = "",
        priority: int = 5,
        depth: int = 0,
        source: str = "planner",
        depends_on: str = "",
        subgoal: str = "",
        max_steps: int = 0,  # 意图级步数预算（0=不限；>0 硬止损）
    ) -> str:
        iid = f"int-{uuid.uuid4().hex[:8]}"
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO intents (id,title,goal,done_criteria,priority,depth,status,
                                        source,depends_on,subgoal,max_steps,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,'open',?,?,?,?,?,?)""",
                (
                    iid,
                    title[:200],
                    goal[:1000],
                    done_criteria[:500],
                    int(priority),
                    int(depth),
                    source,
                    depends_on[:300],
                    subgoal[:300],
                    int(max_steps),
                    now,
                    now,
                ),
            )
        return iid

    def has_open_direct(self) -> bool:
        """存在未完成的直投意图 → 规划者暂停（人工接管模式，ARTEX 模式②）。"""
        with self._conn() as c:
            n = c.execute(
                "SELECT COUNT(*) n FROM intents WHERE source='direct'"
                " AND status IN ('open','running')"
            ).fetchone()["n"]
        return int(n) > 0

    def list_all(self, *, status: str | None = None, limit: int = 50) -> list[dict]:
        sql, args = "SELECT * FROM intents WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY priority DESC, created_at LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def deps_satisfied(self, intent: Intent) -> bool:
        """依赖解锁：depends_on 为空 → 满足；否则逐项检查 intent done / fact 谓词。"""
        raw = intent.depends_on
        if not raw:
            return True
        for dep in raw.split(","):
            dep = dep.strip()
            if not dep:
                continue
            if dep.startswith("fact:"):
                if not self._fact_exists(dep[5:]):
                    return False
            else:
                with self._conn() as c:
                    row = c.execute("SELECT status FROM intents WHERE id=?", (dep,)).fetchone()
                if row is None or row["status"] != "done":
                    return False
        return True

    def _fact_exists(self, predicate: str) -> bool:
        """fact 谓词形如 'credential:admin'（intel 图节点前缀匹配）。"""
        try:
            from secpt.intel.store import IntelStore

            st = IntelStore(Path(os.environ.get("SECPT_VAR", "var")) / "intel.sqlite")
            return bool(st.recall(predicate, limit=1))
        except Exception:
            return False

    def claim_next(self) -> Intent | None:
        """领下一条：priority DESC → depth DESC（深度优先）→ 先创建先领。"""
        with self._conn() as c:
            row = c.execute(
                """SELECT * FROM intents WHERE status='open'
                   ORDER BY priority DESC, depth DESC, created_at LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            c.execute(
                "UPDATE intents SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
        return Intent(
            id=row["id"],
            title=row["title"],
            goal=row["goal"],
            done_criteria=row["done_criteria"],
            priority=row["priority"],
            depth=row["depth"],
            status="running",
            attempts=row["attempts"] + 1,
            # sqlite3.Row 的 `in` 按值迭代不按键——必须 dict 化后 get（老库缺列兼容）
            source=dict(row).get("source", "planner"),
            depends_on=dict(row).get("depends_on", "") or "",
            max_steps=int(dict(row).get("max_steps", 0) or 0),
        )

    def refund_attempt(self, iid: str) -> None:
        """依赖未满足被放回 blocked：回退 claim 的 attempts 消耗（未真正执行）。"""
        with self._conn() as c:
            c.execute(
                "UPDATE intents SET attempts = MAX(attempts-1, 0), updated_at=? WHERE id=?",
                (time.time(), iid),
            )

    def reopen_ready(self) -> int:
        """把依赖已满足的 blocked 意图重新置 open（依赖解锁后恢复可领）。

        依赖未满足的意图在 claim 时置 blocked（不空耗 attempts），
        当被依赖的意图 done / fact 落图后，这里把它们放回可领队列。
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM intents WHERE status='blocked'"
                " ORDER BY priority DESC, depth DESC LIMIT 20"
            ).fetchall()
        n = 0
        for row in rows:
            d = dict(row)
            intent = Intent(
                id=d["id"],
                title=d["title"],
                goal=d["goal"],
                done_criteria=d["done_criteria"],
                priority=d["priority"],
                depth=d["depth"],
                status="blocked",
                attempts=d["attempts"],
                source=d.get("source", "planner"),
                depends_on=d.get("depends_on", "") or "",
            )
            if self.deps_satisfied(intent):
                with self._conn() as c2:
                    c2.execute(
                        "UPDATE intents SET status='open', updated_at=? WHERE id=?",
                        (time.time(), d["id"]),
                    )
                n += 1
        return n

    def settle(self, iid: str, *, status: str, result: str = "", fail_reason: str = "") -> None:
        assert status in ("done", "exhausted", "blocked", "open")
        with self._conn() as c:
            c.execute(
                "UPDATE intents SET status=?, result=?, fail_reason=?, updated_at=? WHERE id=?",
                (status, result[:2000], fail_reason[:500], time.time(), iid),
            )

    def count_open(self) -> int:
        with self._conn() as c:
            return int(
                c.execute("SELECT COUNT(*) n FROM intents WHERE status='open'").fetchone()["n"]
            )

    def total(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) n FROM intents").fetchone()["n"])

    def recent_results(self, limit: int = 5) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT id,title,status,result,fail_reason FROM intents
                   WHERE status IN ('done','exhausted','blocked') AND result != ''
                   ORDER BY updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def situation_report(self, *, limit: int = 6) -> dict:
        """结构化态势：planner 的决策原材料（对齐 ARTEX 双图注入）。

        比文本摘要多的三样：intel 图最新事实/发现（跨任务记忆）、
        未触达资产（覆盖度缺口）、否定记录（防重复踩坑已有）。
        """
        report: dict = {
            "open_intents": self.open_summary(limit),
            "recent_results": self.recent_results(limit),
            "subgoal_coverage": self.subgoal_coverage(),
        }
        try:
            from secpt.intel.store import IntelStore

            # 调用时读 SECPT_VAR（模块级 DEFAULT_DB 是 import 期快照，测试/多实例会漂移）
            st = IntelStore(Path(os.environ.get("SECPT_VAR", "var")) / "intel.sqlite")
            with st._conn() as c:
                rows = c.execute(
                    """SELECT id,type,content FROM nodes
                       WHERE type IN ('fact','finding','hint') AND COALESCE(cold,0)=0
                       ORDER BY ts DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            report["recent_facts"] = [
                {"id": r["id"], "type": r["type"], "content": str(r["content"])[:200]} for r in rows
            ]
            report["untested_assets"] = [
                {"id": a["id"], "value": str(a["value"])[:100]}
                for a in st.untested_assets(atype="endpoint", limit=5)
            ]
        except Exception:
            report["recent_facts"] = []
            report["untested_assets"] = []
        return report

    def open_summary(self, limit: int = 10) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT id,title,priority,depth FROM intents
                   WHERE status='open' ORDER BY priority DESC, depth DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def subgoal_coverage(self, limit: int = 10) -> list[dict]:
        """Goals 层覆盖度：按子目标聚合意图结算状态（planner 决策原材料）。

        每个子目标给出 done/exhausted/open 计数——planner 据此判断
        "哪些方向已证、哪些没测完"，跨轮方向一致性从文本记忆变结构记忆。
        """
        with self._conn() as c:
            rows = c.execute(
                """SELECT subgoal,
                          SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
                          SUM(CASE WHEN status='exhausted' THEN 1 ELSE 0 END) AS exhausted,
                          SUM(CASE WHEN status IN ('open','running','blocked')
                              THEN 1 ELSE 0 END) AS pending
                   FROM intents WHERE subgoal != ''
                   GROUP BY subgoal ORDER BY pending DESC, done DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["done"] = int(d["done"] or 0)
            d["exhausted"] = int(d["exhausted"] or 0)
            d["pending"] = int(d["pending"] or 0)
            out.append(d)
        return out

    def all_subgoals_settled(self) -> bool:
        """覆盖度驱动收敛判定：所有子目标下意图均已结算（done/exhausted）。

        存在 subgoal 分组且每个分组都无未结算意图 → True。
        没有任何 subgoal 时返回 False（退回 idle_rounds 空转逻辑，向后兼容）。
        """
        with self._conn() as c:
            row = c.execute(
                """SELECT COUNT(*) n FROM (
                       SELECT subgoal FROM intents WHERE subgoal != ''
                       GROUP BY subgoal
                       HAVING SUM(CASE WHEN status IN ('open','running','blocked')
                                       THEN 1 ELSE 0 END) > 0
                   )"""
            ).fetchone()
            total = c.execute(
                "SELECT COUNT(DISTINCT subgoal) n FROM intents WHERE subgoal != ''"
            ).fetchone()
        return int(total["n"]) > 0 and int(row["n"]) == 0


_PLANNER_PROMPT = """你是渗透任务的 planner。根据【总目标】【近期结果】【当前待办】产出下一步意图。

纪律（必须遵守）：
1. 深度优先：优先产出能推进当前最深利用链的意图，不要浅层铺开
2. 否定记账：近期结果里已证伪的路线不要重复派意图（至多复核一次）
3. 每条意图必须可判定：done_criteria 写清"怎样算这条做完了"
4. 最多产 {max_new} 条；没有值得做的就产空列表（任务自然收敛）
5. 子目标记账：每条意图可用可选字段 "subgoal" 归属到某个子目标；优先派发
   【子目标覆盖度】里 pending=0 且 done=0 的未测方向；某子目标已全部 done 就别再派

【总目标】
{objective}

【近期结果】
{results}

【当前待办】
{opens}

【图记忆·最新事实/发现（跨任务）】
{facts}

【覆盖度缺口·未触达资产（优先派这里）】
{untested}

【子目标覆盖度】
{coverage}

只输出 JSON：[{{"title": "...", "goal": "...", "done_criteria": "...",
"priority": 1-10, "depth": {next_depth}, "subgoal": "..."}}]"""


async def plan_round(
    objective: str,
    frontier: Frontier,
    llm,
    *,
    model: str = "deepseek-chat",
    max_new: int = 3,
    next_depth: int = 0,
) -> int:
    """planner 新会话产意图。解析失败/无 llm 返回 0（调度继续，不阻塞）。"""
    situation = frontier.situation_report()
    prompt = _PLANNER_PROMPT.format(
        max_new=max_new,
        objective=objective[:1500],
        results=json.dumps(situation["recent_results"], ensure_ascii=False)[:1200] or "（无）",
        opens=json.dumps(situation["open_intents"], ensure_ascii=False)[:600] or "（无）",
        next_depth=next_depth,
        facts=json.dumps(situation.get("recent_facts", []), ensure_ascii=False)[:900] or "（无）",
        untested=json.dumps(situation.get("untested_assets", []), ensure_ascii=False)[:400]
        or "（无）",
        coverage=json.dumps(situation.get("subgoal_coverage", []), ensure_ascii=False)[:500]
        or "（无）",
    )
    try:
        if hasattr(llm, "call"):
            resp = await llm.call([{"role": "user", "content": prompt}], [], model)
        else:
            resp = await llm([{"role": "user", "content": prompt}], [], model, "")
        content = (resp.choices[0].message.content or "").strip()
    except Exception:
        return 0
    start, end = content.find("["), content.rfind("]")
    if start < 0 or end <= start:
        return 0
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return 0
    n = 0
    for item in data[:max_new]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        subgoal = str(item.get("subgoal") or "").strip()
        frontier.add(
            title,
            goal=str(item.get("goal") or ""),
            done_criteria=str(item.get("done_criteria") or ""),
            priority=int(item.get("priority", 5)),
            depth=int(item.get("depth", next_depth)),
            subgoal=subgoal,
        )
        if subgoal:
            # Goals 层：子目标落探索图（幂等；best-effort，失败不阻断派发）
            with contextlib.suppress(Exception):
                from secpt.intel.store import IntelStore

                IntelStore(Path(os.environ.get("SECPT_VAR", "var")) / "intel.sqlite").record_goal(
                    subgoal
                )
        n += 1
    return n


def build_intent_objective(global_objective: str, intent: Intent, recent: list[dict]) -> str:
    recent_txt = "\n".join(
        f"- [{r['status']}] {r['title']}: {r['result'][:200]}" for r in recent[:4]
    )
    return f"""【总目标】
{global_objective}

【当前意图】{intent.title}
目标：{intent.goal}
完成判据：{intent.done_criteria or "拿到该意图的结论即可"}

【近期相关结果（避免重复踩坑）】
{recent_txt or "（无）"}

要求：只推进当前意图，完成判据满足后以 ANSWER: <结论> 收尾。"""


async def run_frontier(
    objective: str,
    *,
    profile: str = "hunter",
    base_job_id: str | None = None,
    max_rounds: int = 20,
    max_intents: int = 60,
    llm=None,
    model: str | None = None,  # 缺省 SECPT_MODEL_PRIMARY > deepseek-chat
    planner_llm=None,  # 缺省与 worker 同模型；可分开（强模型规划+便宜模型执行）
    frontier: Frontier | None = None,
    max_workers: int = 1,  # D2.2 并发 worker 数（ARTEX 默认 3；>1 需事件唤醒）
    wake_queue: object | None = None,  # D2.3 外部触发队列（settle 时 put intent_id）
) -> dict:
    """意图驱动主循环。返回汇总（chains/flags/wall）。

    模式语义：
    - 队列中存在未完成的 direct（人工直投）意图 → 规划者暂停，只执行 direct，
      全部 direct 完成且无新 direct → 结束（ARTEX 模式②）；此后恢复正常规划
    - D2.2：max_workers>1 时并发跑多个 claim→run→settle 循环（原子认领）
    """
    import asyncio

    from secpt.cli.engine_runner import resolve_models, run_engine

    model, _fb = resolve_models(model, None)

    # llm 缺省时解析生产 LLM——planner 与 worker 共用（懒升级路径此前传 None 导致
    # planner 静默死亡：plan_round 捕获 AttributeError 返回 0 意图）
    if llm is None or planner_llm is None:
        from secpt.cli.engine_runner import _prod_llm

        _prod = _prod_llm()
        llm = llm or _prod
        planner_llm = planner_llm or _prod

    fr = frontier or Frontier()
    base = base_job_id or f"hunt-{uuid.uuid4().hex[:8]}"
    chains: list[dict] = []
    flags: list[str] = []
    chains_lock = asyncio.Lock()
    idle_rounds = 0
    t0 = time.time()

    async def _maybe_plan() -> int:
        """规划者轮：人工接管（direct 未完成）时暂停；否则产意图。"""
        nonlocal idle_rounds
        if fr.has_open_direct():
            return 0  # 模式②：规划者暂停
        if fr.total() >= max_intents or idle_rounds >= 2:
            return 0
        with fr._conn() as c:
            row = c.execute("SELECT MAX(depth) d FROM intents").fetchone()
            max_depth = int(row["d"] or 0)
        n_new = await plan_round(
            objective,
            fr,
            planner_llm,
            model=model,
            next_depth=max_depth + (1 if fr.total() else 0),
        )
        idle_rounds = 0 if n_new else idle_rounds + 1
        return n_new

    async def _run_one(intent: Intent) -> str | None:
        """执行单条意图并结算。返回 answer（有成果时非空）。"""
        # 意图级步数预算：硬止损（收益递减机械化）——意图预算优先于全局 profile
        _kw = {"max_steps": intent.max_steps} if getattr(intent, "max_steps", 0) > 0 else {}
        result = await asyncio.to_thread(
            run_engine,
            objective=build_intent_objective(objective, intent, fr.recent_results(4)),
            profile=profile,
            job_id=f"{base}-i{intent.id[-4:]}",
            llm=llm,
            model_primary=model,
            **_kw,
        )
        answer = result.flag or ""
        if answer:
            status, why = "done", ""
        elif getattr(intent, "max_steps", 0) > 0 and result.status == "step_exceeded":
            status, why = "exhausted", f"超意图步数预算（{intent.max_steps} 步硬止损）"
        elif result.status in ("step_exceeded", "wallclock_exceeded") and intent.attempts >= 2:
            status, why = "exhausted", f"{result.status}（尝试 {intent.attempts} 次）"
        elif result.status == "error":
            status, why = "blocked", "engine error"
        else:
            status, why = "exhausted", f"{result.status}（首次失败，可复核一次）"
        fr.settle(
            intent.id, status=status, result=answer or f"steps={result.steps}", fail_reason=why
        )
        fr.reopen_ready()  # 本条结算后立即解锁依赖已满足的 blocked 意图（事件驱动，不等轮询）
        async with chains_lock:
            chains.append(
                {
                    "intent": intent.title,
                    "status": status,
                    "steps": result.steps,
                    "depth": intent.depth,
                    "source": intent.source,
                }
            )
        if wake_queue is not None:
            with contextlib.suppress(Exception):  # D2.3 事件唤醒 planner
                wake_queue.put_nowait(intent.id)
        return answer or None

    stop = False
    wake_q: asyncio.Queue = wake_queue if wake_queue is not None else asyncio.Queue()
    stop_ev = asyncio.Event()

    async def _worker_loop() -> None:
        nonlocal stop
        while not stop_ev.is_set():
            intent = fr.claim_next()
            if intent is None:
                fr.reopen_ready()  # 无 open 可领 → 解锁依赖已满足的 blocked 意图
                await asyncio.sleep(0.5)
                continue
            if not fr.deps_satisfied(intent):  # D2.1 依赖未满足 → blocked（不空耗 attempts）
                fr.settle(intent.id, status="blocked", fail_reason="依赖未满足")
                fr.refund_attempt(intent.id)
                await asyncio.sleep(0.5)
                continue
            ans = await _run_one(intent)
            if ans:
                flags.append(ans)
                stop = True
                stop_ev.set()  # 拿到成果即整体收敛

    workers = [asyncio.create_task(_worker_loop()) for _ in range(max_workers)]

    # 事件驱动 planner：worker settle / 心跳 唤醒（对齐 ARTEX"数据库变化唤醒"）
    idle_rounds = 0
    rounds = 0
    while rounds < max_rounds and not stop:
        if fr.has_open_direct():
            # 模式②：只执行 direct；队列清空且无 running → 结束
            if fr.count_open() == 0:
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake_q.get(), timeout=2.0)
            continue
        starving = fr.count_open() < max_workers
        if fr.total() < max_intents and idle_rounds < 2 and (starving or fr.count_open() == 0):
            await _maybe_plan()
            rounds += 1
        if fr.count_open() == 0:
            if fr.total() == 0 and rounds == 0:
                continue  # 首盘：等 worker 空转也无妨，继续 plan
            if fr.all_subgoals_settled():
                break  # Goals 覆盖度驱动收敛：所有子目标已结算（done/exhausted）
            if idle_rounds >= 2:
                break
            timeout = 2.0  # 空队列短心跳（测试友好；实战 2s 无害）
        else:
            timeout = float(os.environ.get("SECPT_FRONTIER_HEARTBEAT", "60"))
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(wake_q.get(), timeout=timeout)

    stop_ev.set()
    await asyncio.gather(*workers, return_exceptions=True)

    return {
        "job_id": base,
        "mode": "frontier",
        "flags": flags,
        "chains": chains,
        "open_remaining": fr.count_open(),
        "wall_s": round(time.time() - t0, 1),
    }
