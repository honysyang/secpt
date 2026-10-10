"""planner/frontier：意图驱动的深度优先调度（多步利用链不丢计划）。

CHECKMATE/HPTSA 的可迁移最小实现：
- planner 每轮全新会话（省 context、防漂移），只负责产意图（intent）入 frontier
- frontier 存 SQLite——常驻 todolist 跨 resume/重启持久化（比 ARTEX 内存版强，天然配 7×24）
- worker = 现有单链 Engine：领一条 intent 跑到自宣告，计划本身不依赖模型上下文记忆
- 深度优先 + 否定记账：intent 有 depth 与 attempts，失败两次即 exhausted（至多复核一次）
"""

from __future__ import annotations

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


class Frontier:
    """意图队列（SQLite 持久化 todolist）。"""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

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
    ) -> str:
        iid = f"int-{uuid.uuid4().hex[:8]}"
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO intents (id,title,goal,done_criteria,priority,depth,status,
                                        created_at,updated_at)
                   VALUES (?,?,?,?,?,?,'open',?,?)""",
                (
                    iid,
                    title[:200],
                    goal[:1000],
                    done_criteria[:500],
                    int(priority),
                    int(depth),
                    now,
                    now,
                ),
            )
        return iid

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
        )

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

    def open_summary(self, limit: int = 10) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT id,title,priority,depth FROM intents
                   WHERE status='open' ORDER BY priority DESC, depth DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


_PLANNER_PROMPT = """你是渗透任务的 planner。根据【总目标】【近期结果】【当前待办】产出下一步意图。

纪律（必须遵守）：
1. 深度优先：优先产出能推进当前最深利用链的意图，不要浅层铺开
2. 否定记账：近期结果里已证伪的路线不要重复派意图（至多复核一次）
3. 每条意图必须可判定：done_criteria 写清"怎样算这条做完了"
4. 最多产 {max_new} 条；没有值得做的就产空列表（任务自然收敛）

【总目标】
{objective}

【近期结果】
{results}

【当前待办】
{opens}

只输出 JSON：[{{"title": "...", "goal": "...", "done_criteria": "...",
"priority": 1-10, "depth": {next_depth}}}]"""


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
    results = frontier.recent_results(5)
    opens = frontier.open_summary(10)
    prompt = _PLANNER_PROMPT.format(
        max_new=max_new,
        objective=objective[:1500],
        results=json.dumps(results, ensure_ascii=False)[:1500] or "（无）",
        opens=json.dumps(opens, ensure_ascii=False)[:800] or "（无）",
        next_depth=next_depth,
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
        frontier.add(
            title,
            goal=str(item.get("goal") or ""),
            done_criteria=str(item.get("done_criteria") or ""),
            priority=int(item.get("priority", 5)),
            depth=int(item.get("depth", next_depth)),
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
    model: str = "deepseek-chat",
    planner_llm=None,  # 缺省与 worker 同模型；可分开（强模型规划+便宜模型执行）
    frontier: Frontier | None = None,
) -> dict:
    """意图驱动主循环。返回汇总（chains/flags/wall）。"""
    import asyncio

    from secpt.cli.engine_runner import run_engine

    fr = frontier or Frontier()
    base = base_job_id or f"hunt-{uuid.uuid4().hex[:8]}"
    chains: list[dict] = []
    flags: list[str] = []
    idle_rounds = 0
    t0 = time.time()

    for _round in range(max_rounds):
        if fr.total() < max_intents and idle_rounds < 2:
            max_depth = 0
            with fr._conn() as c:
                row = c.execute("SELECT MAX(depth) d FROM intents").fetchone()
                max_depth = int(row["d"] or 0)
            n_new = await plan_round(
                objective,
                fr,
                planner_llm or llm,
                model=model,
                next_depth=max_depth + (1 if fr.total() else 0),
            )
            idle_rounds = 0 if n_new else idle_rounds + 1
        intent = fr.claim_next()
        if intent is None:
            if idle_rounds >= 2:
                break
            idle_rounds += 1
            continue
        result = await asyncio.to_thread(
            run_engine,
            objective=build_intent_objective(objective, intent, fr.recent_results(4)),
            profile=profile,
            job_id=f"{base}-i{intent.id[-4:]}",
            llm=llm,
            model_primary=model,
        )
        answer = result.flag or ""
        if answer:
            status, why = "done", ""
            flags.append(answer)
        elif result.status in ("step_exceeded", "wallclock_exceeded") and intent.attempts >= 2:
            status, why = "exhausted", f"{result.status}（尝试 {intent.attempts} 次）"
        elif result.status == "error":
            status, why = "blocked", "engine error"
        else:
            status, why = ("exhausted", f"{result.status}（首次失败，可复核一次）")
        fr.settle(
            intent.id, status=status, result=answer or f"steps={result.steps}", fail_reason=why
        )
        chains.append(
            {"intent": intent.title, "status": status, "steps": result.steps, "depth": intent.depth}
        )
        if answer:  # 拿到成果即整体收敛（调用人可决定是否继续扩面）
            break

    return {
        "job_id": base,
        "mode": "frontier",
        "flags": flags,
        "chains": chains,
        "open_remaining": fr.count_open(),
        "wall_s": round(time.time() - t0, 1),
    }
