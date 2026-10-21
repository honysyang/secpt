"""假设树：Commander 的世界模型（结构化信念存储，v4）。

设计出处：工作循环形态 v4——主智能体无会话状态，信念外置在两个载体：
战略文档（strategy.md，Commander 自己写自己读）与假设树（本模块，结构化）。

节点状态机（唯一合法迁移）：
    proposed → testing → confirmed
                       → refuted
             testing  → refined（生子节点，父节点标记 refined）
             任意     → dead（预算耗尽放弃，留否定记录）

纪律（硬校验，防"任务池换皮"退化）：
- 每个假设必须带可证伪陈述 statement 与判定实验 test（"什么结果算证实/证伪"）；
- confirmed / refuted 必须附证据指针（blob 哈希 / 文件路径 / 意图 job id），
  无指针拒绝迁移——结论必须证据锚定。

存储：var/jobs/<base>/hypothesis.sqlite（按任务隔离，与 frontier.sqlite 同目录）。
Watch 层在意图结算时落确定性信念增量（record_evidence_event），Commander 唤醒时
做精细结算（beliefs 迁移）——两层共写一棵树，状态机保证不出现非法状态。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field

STATES = ("proposed", "testing", "confirmed", "refuted", "dead")

# 合法迁移表：from → 允许的 to 集合
_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"testing", "dead"},
    "testing": {"confirmed", "refuted", "dead"},
    "confirmed": set(),  # 终态（refined 通过 refine() 专用路径：父标 refined）
    "refuted": set(),  # 终态
    "dead": set(),  # 终态
}
# refined 只能从 testing 经 refine() 进入（父节点专用，不在普通迁移表里）
_REFINEABLE = {"proposed", "testing"}


class HypothesisError(ValueError):
    """非法假设操作（状态机违例 / 缺证据指针 / 陈述不可证伪）。"""


@dataclass
class Hypothesis:
    id: str
    title: str
    statement: str  # 可证伪陈述
    test: str  # 判定实验：什么结果算证实 / 证伪
    state: str = "proposed"
    confidence: float = 0.5
    parent_id: str = ""
    evidence_refs: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "statement": self.statement,
            "test": self.test,
            "state": self.state,
            "confidence": self.confidence,
            "parent_id": self.parent_id,
            "evidence_refs": list(self.evidence_refs),
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    statement TEXT DEFAULT '',
    test TEXT DEFAULT '',
    state TEXT DEFAULT 'proposed',
    confidence REAL DEFAULT 0.5,
    parent_id TEXT DEFAULT '',
    evidence_refs TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hid TEXT DEFAULT '',
    ref TEXT DEFAULT '',
    note TEXT DEFAULT '',
    consumed INTEGER DEFAULT 0,
    ts REAL NOT NULL
);
"""


def hypothesis_db_path(base_job_id: str):
    """任务级假设库路径（与 frontier.sqlite 同目录，按任务隔离）。"""
    from secpt.core.job.resume import jobs_dir

    return jobs_dir() / base_job_id / "hypothesis.sqlite"


def _validate_content(title: str, statement: str, test: str) -> None:
    """入口纪律：可证伪陈述与判定实验不得为空（防假设树退化成任务池换皮）。"""
    if not title.strip():
        raise HypothesisError("假设标题不能为空")
    if len(statement.strip()) < 8:
        raise HypothesisError(f"假设陈述不可证伪（太短/缺失）：{statement[:40]!r}")
    if len(test.strip()) < 4:
        raise HypothesisError(f"缺判定实验（什么结果算证实/证伪）：{test[:40]!r}")


class HypothesisTree:
    """假设树（SQLite 持久化）。所有迁移走状态机校验。"""

    def __init__(self, db_path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ---------------- 写 ----------------

    def add(
        self,
        title: str,
        *,
        statement: str,
        test: str,
        parent_id: str = "",
        confidence: float = 0.5,
        evidence_refs: list[str] | None = None,
    ) -> str:
        _validate_content(title, statement, test)
        if parent_id:
            parent = self.get(parent_id)
            if parent is None:
                raise HypothesisError(f"父假设不存在：{parent_id}")
            if parent.state not in _REFINEABLE:
                raise HypothesisError(f"父假设 {parent_id} 状态 {parent.state} 不可细化")
        hid = f"H-{uuid.uuid4().hex[:6]}"
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO hypotheses
                   (id,title,statement,test,state,confidence,parent_id,evidence_refs,
                    created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    hid,
                    title[:200],
                    statement[:1000],
                    test[:500],
                    "proposed",
                    min(1.0, max(0.0, float(confidence))),
                    parent_id,
                    "|".join(r for r in (evidence_refs or []) if r)[:500],
                    now,
                    now,
                ),
            )
            if parent_id:
                # refined 是父节点专用迁移：子节点落地的同时父节点退出活动集
                c.execute(
                    "UPDATE hypotheses SET state='refined', updated_at=? WHERE id=? AND state IN"
                    " ('proposed','testing')",
                    (now, parent_id),
                )
        return hid

    def get(self, hid: str) -> Hypothesis | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM hypotheses WHERE id=?", (hid,)).fetchone()
        if row is None:
            return None
        return Hypothesis(
            id=row["id"],
            title=row["title"],
            statement=row["statement"],
            test=row["test"],
            state=row["state"],
            confidence=float(row["confidence"] or 0.5),
            parent_id=row["parent_id"] or "",
            evidence_refs=[r for r in (row["evidence_refs"] or "").split("|") if r],
            created_at=float(row["created_at"] or 0),
            updated_at=float(row["updated_at"] or 0),
        )

    def set_state(
        self, hid: str, state: str, *, evidence_ref: str = "", confidence: float | None = None
    ) -> bool:
        """状态迁移（状态机校验；confirmed/refuted 必须附证据指针）。"""
        if state not in STATES:
            raise HypothesisError(f"未知状态 {state!r}")
        node = self.get(hid)
        if node is None:
            return False
        if state == node.state:
            return True
        if state == "refined":
            raise HypothesisError("refined 只能经 refine() 产生")
        allowed = _TRANSITIONS.get(node.state, set())
        if state not in allowed:
            raise HypothesisError(f"非法迁移 {node.state} → {state}（{hid}）")
        if state in ("confirmed", "refuted") and not (evidence_ref or node.evidence_refs):
            raise HypothesisError(f"{state} 必须附证据指针（{hid}）")
        now = time.time()
        with self._conn() as c:
            sets = ["state=?", "updated_at=?"]
            args: list = [state, now]
            if evidence_ref:
                sets.append("evidence_refs=?")
                merged = "|".join([*node.evidence_refs, evidence_ref])[:500]
                args.append(merged)
            if confidence is not None:
                sets.append("confidence=?")
                args.append(min(1.0, max(0.0, float(confidence))))
            args.append(hid)
            c.execute(f"UPDATE hypotheses SET {', '.join(sets)} WHERE id=?", args)
        return True

    def update_confidence(self, hid: str, confidence: float) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE hypotheses SET confidence=?, updated_at=? WHERE id=?",
                (min(1.0, max(0.0, float(confidence))), time.time(), hid),
            )
            return cur.rowcount > 0

    def abandon(self, hid: str, reason: str = "") -> bool:
        """预算耗尽放弃：任意活动状态 → dead，留否定记录。"""
        node = self.get(hid)
        if node is None:
            return False
        if node.state not in ("proposed", "testing"):
            return False
        ref = reason or "budget-exhausted"
        return self.set_state(hid, "dead", evidence_ref=ref)

    def record_evidence_event(self, hid: str, ref: str, note: str = "") -> None:
        """Watch 层落未消化证据（Commander 唤醒时逐条映射到信念）。"""
        with self._conn() as c:
            c.execute(
                "INSERT INTO evidence_events (hid,ref,note,consumed,ts) VALUES (?,?,?,0,?)",
                ((hid or "")[:40], (ref or "")[:300], (note or "")[:500], time.time()),
            )

    def undigested_evidence(self, limit: int = 10) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id,hid,ref,note,ts FROM evidence_events WHERE consumed=0"
                " ORDER BY ts LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_consumed(self, ids: list[int]) -> None:
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        with self._conn() as c:
            c.execute(f"UPDATE evidence_events SET consumed=1 WHERE id IN ({marks})", ids)

    # ---------------- 读（简报编译器的数据源） ----------------

    def open_high_value(self, *, floor: float = 0.1) -> list[Hypothesis]:
        """活动假设（proposed/testing），置信度降序——收敛判据的数据源。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM hypotheses WHERE state IN ('proposed','testing')"
                " AND confidence >= ? ORDER BY confidence DESC",
                (floor,),
            ).fetchall()
        return [self.get(r["id"]) for r in rows]

    def nodes_by_state(self, *states: str) -> list[Hypothesis]:
        """按状态取节点（如 confirmed——交付回滚的查询口径）。"""
        if not states:
            return []
        marks = ",".join("?" * len(states))
        with self._conn() as c:
            rows = c.execute(
                f"SELECT id FROM hypotheses WHERE state IN ({marks}) ORDER BY confidence DESC",
                tuple(states),
            ).fetchall()
        return [self.get(r["id"]) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute("SELECT state, COUNT(*) n FROM hypotheses GROUP BY state").fetchall()
        return {r["state"]: int(r["n"]) for r in rows}

    def digest(self, limit: int = 14) -> list[dict]:
        """树当前形态（简报编译器用）：活动优先，终态压缩。"""
        with self._conn() as c:
            rows = c.execute(
                """SELECT id,title,state,confidence,test FROM hypotheses
                   ORDER BY CASE state WHEN 'testing' THEN 0 WHEN 'proposed' THEN 1
                            ELSE 2 END, confidence DESC, updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "state": r["state"],
                "confidence": round(float(r["confidence"] or 0.5), 2),
                "test": (r["test"] or "")[:120],
            }
            for r in rows
        ]

    def total(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) n FROM hypotheses").fetchone()["n"])
