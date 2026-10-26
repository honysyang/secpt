"""StoreView：只读投影（ARCHITECTURE §8 L1；裁定 2 / 裁定 3 的落点）。

restore() = 快照（可选）+ 事件重放 + derive_stats 全量重算 meta。
证据库布局（本模块与 S4 infra/evidence.put 的契约）：
  evidence/<sha[:2]>/<sha>.json         内容（写入时的原始字节）
  evidence/<sha[:2]>/<sha>.meta.json    元数据 {"id", "sha", "task_id", "ts", ...}
  id = "ev-" + sha256(content)[:16]
证据不可变，故元数据与校验结果可安全 LRU 缓存（裁定 3）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from pa.errors import SecptError
from pa.models import Clue, Event, MissionSpec, MissionState
from pa.store.derive import derive_stats
from pa.store.event import apply_event


class StoreView:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._state: MissionState | None = None

    def exists(self) -> bool:
        return (self.root / "events.jsonl").exists()

    def events(self, from_seq: int = 1) -> Iterator[Event]:
        p = self.root / "events.jsonl"
        if not p.exists():
            return
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = Event.model_validate_json(line)
                if ev.seq >= from_seq:
                    yield ev

    def restore(self) -> MissionState:
        snap = self.root / "snapshot.json"
        start = 1
        if snap.exists():
            blob = json.loads(snap.read_text(encoding="utf-8"))
            state = MissionState.model_validate(blob["state"])
            start = int(blob["seq"]) + 1
        else:
            state = self._base_state()
        for ev in self.events(from_seq=start):
            apply_event(state, ev)
        # 行为统计只从账本重算（裁定 2）：快照里的旧值一律被覆盖
        state.meta.update(derive_stats(self.events(from_seq=1)))
        self._state = state
        return state

    def mission(self) -> MissionState:
        if self._state is None:
            self._state = self.restore() if self.exists() else self._base_state()
        return self._state

    def evidence(self, eid: str) -> dict:
        text = _load_content(str(self.root), eid)
        if text is None:
            raise SecptError(f"evidence not found: {eid}")
        return json.loads(text)

    def evidence_meta(self, eid: str) -> dict:
        return _load_meta(str(self.root), eid)

    def evidence_verified(self, eid: str) -> bool:
        return _verified(str(self.root), eid)

    def _base_state(self) -> MissionState:
        mp = self.root / "mission.json"
        if not mp.exists():
            raise SecptError(f"mission.json not found under {self.root}")
        spec = MissionSpec.model_validate(json.loads(mp.read_text(encoding="utf-8")))
        return MissionState(spec=spec, mission_id=self.root.name)


def _find(root: Path, eid: str) -> tuple[Path, Path] | None:
    if not eid.startswith("ev-"):
        return None
    prefix = eid[3:]
    bucket = root / "evidence" / prefix[:2]
    if not bucket.is_dir():
        return None
    for c in sorted(bucket.glob(prefix + "*.json")):
        if c.name.endswith(".meta.json"):
            continue
        sha = c.name[: -len(".json")]
        return c, bucket / f"{sha}.meta.json"
    return None


@lru_cache(maxsize=256)
def _load_content(root_str: str, eid: str) -> str | None:
    found = _find(Path(root_str), eid)
    if found is None:
        return None
    return found[0].read_text(encoding="utf-8")


@lru_cache(maxsize=256)
def _load_meta(root_str: str, eid: str) -> dict:
    found = _find(Path(root_str), eid)
    if found is None or not found[1].exists():
        return {}
    return json.loads(found[1].read_text(encoding="utf-8"))


@lru_cache(maxsize=256)
def _verified(root_str: str, eid: str) -> bool:
    found = _find(Path(root_str), eid)
    if found is None:
        return False
    raw = found[0].read_bytes()
    return hashlib.sha256(raw).hexdigest() == found[0].stem


# ── 线索视图（E1「store 查询视图巩固」：统一经 store 读、按类型过滤、
#    索引分层——WORKFLOW §7 E1 / §9.2-1）。纯函数：吃投影里的线索列表，
#    不碰 IO，谁渲染谁调，策略（该看哪类）归调用方。─────────────


def filter_clues(
    clues: list[Clue], kinds: set[str] | None = None, window: int = 60
) -> list[Clue]:
    """类型过滤 + 末 window 条窗口，一体两用（E1「按类型过滤」）。

    kinds 非空时**先按类型过滤再取窗口**：否则同一 kind 高产会把别类
    线索整个挤出正文窗口（全 kind 混流取末 60 的已知毛病）；kinds 为
    None 时行为与旧窗口全等——老调用方零迁移。window<=0 返回全量。
    """
    picked = [c for c in clues if not kinds or c.kind in kinds]
    return picked[-window:] if window > 0 else picked


def clue_summary(clues: list[Clue]) -> dict[str, dict]:
    """L2 索引视图（ WORKFLOW §9.2-1「索引当独立视图结构：key+summary+溯源边」）。

    按 kind 聚合计数 + key 身份样本（去重、首现序、<=5 个——同一个 key
    出现两次没有信息量，样本要的是「这类线索都长什么样」的多样性）+
    溯源（source_task 集合）。编排者据此判断该派什么活、worker 去哪验证，
    不内联每条全值——正文窗口属于执行角色，编排层只看账面积貌。
    返回 {kind: {count, keys, source_tasks}}；kind 排序由调用方渲染时做。
    """
    idx: dict[str, dict] = {}
    for c in clues:
        e = idx.setdefault(
            c.kind, {"count": 0, "keys": [], "seen": set(), "source_tasks": set()}
        )
        e["count"] += 1
        if c.key not in e["seen"] and len(e["keys"]) < 5:
            e["seen"].add(c.key)
            e["keys"].append(c.key)
        if c.source_task:
            e["source_tasks"].add(c.source_task)
    return {
        kind: {
            "count": int(e["count"]),
            "keys": list(e["keys"]),
            "source_tasks": sorted(e["source_tasks"]),
        }
        for kind, e in idx.items()
    }
