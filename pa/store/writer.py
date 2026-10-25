"""StoreWriter：事件账本追加 + 快照 + 动作指纹索引（ARCHITECTURE §8 L1）。

只有 runtime 持有写接口（§2.3 单写者）。每条事件 fsync（§5.2 硬要求 1）；
seq 单调，启动扫描账本恢复游标与索引（§5.2 硬要求 3）；动作指纹在
action.started 时登记，重放时同指纹不再执行（§7.6 幂等保证）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from pa.models import Event, MissionState

SNAPSHOT_EVERY = 200


class StoreWriter:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.events_path = self.root / "events.jsonl"
        self._seq = 0
        self._idem: dict[str, Event] = {}
        self._actions: dict[str, str | None] = {}
        self._exits: dict[str, int] = {}
        self._scan()

    def _scan(self) -> None:
        if not self.events_path.exists():
            return
        with self.events_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = Event.model_validate_json(line)
                self._seq = max(self._seq, ev.seq)
                self._idem.setdefault(ev.idem_key, ev)
                self._index(ev)

    def _index(self, ev: Event) -> None:
        """动作指纹索引：同指纹不再执行（§7.6 幂等保证）。"""
        if ev.type == "action.started":
            fp = ev.payload.get("fp")
            if fp:
                self._actions[fp] = None
        elif ev.type == "action.finished":
            fp = ev.payload.get("fp")
            if fp:
                self._actions[fp] = ev.payload.get("evidence")
                self._exits[fp] = int(ev.payload.get("exit", 0))

    def append(self, type: str, actor: str, payload: dict, idem_key: str) -> Event:
        if idem_key in self._idem:              # 幂等：同 key 直接跳过
            return self._idem[idem_key]
        self._seq += 1
        ev = Event(seq=self._seq, mission_id=self.root.name, type=type,
                   actor=actor, payload=payload, idem_key=idem_key)
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(ev.model_dump_json() + "\n")
            f.flush()
            os.fsync(f.fileno())                # ★必须 fsync
        self._idem[idem_key] = ev
        self._index(ev)                         # 本会话 append 也要维护索引
        return ev

    def checkpoint(self, state: MissionState) -> None:
        if self._seq % SNAPSHOT_EVERY == 0:
            self.snapshot_now(state)

    def snapshot_now(self, state: MissionState) -> None:
        blob = json.dumps({"seq": self._seq, "state": state.model_dump(mode="json")},
                          ensure_ascii=False)
        tmp = self.root / "snapshot.json.tmp"
        with tmp.open("w", encoding="utf-8") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.root / "snapshot.json")
        dfd = os.open(self.root, os.O_RDONLY)   # rename 后刷目录项，防掉电丢名
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def seen_action(self, fp: str) -> bool:
        return fp in self._actions

    def action_evidence(self, fp: str) -> str | None:
        return self._actions.get(fp)

    def action_exit(self, fp: str) -> int | None:
        """该指纹最后一次执行的退出码（action.finished 索引）。"""
        return self._exits.get(fp)
