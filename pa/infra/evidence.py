"""证据盒：内容寻址存储（BUILD_SPEC §7.2；布局契约由 test_store_recovery 锁定）。

布局：evidence/<sha[:2]>/<sha>.json（原始内容字节）+ <sha>.meta.json
（{"id","sha","task_id","ts",...extra}），id = "ev-" + sha256(content)[:16]。
内容不可变 → StoreView 侧可 LRU 缓存。

写盘一律原子：tmp + fsync + os.replace + 目录 fsync。半写中断（残留 tmp
或内容截断）后重放同一内容可自愈——put 先校验已有内容，损坏则原子重写
（S4 追加验收一）。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pa.models import now

_ID_PREFIX = "ev-"


def _dig(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _dir(root: Path, sha: str) -> Path:
    return Path(root) / "evidence" / sha[:2]


def _find_content(root: Path, sha16: str) -> Path | None:
    """按 id 里的 16 位前缀找内容文件（跳过 *.meta.json）。"""
    d = Path(root) / "evidence" / sha16[:2]
    if not d.is_dir():
        return None
    for p in sorted(d.glob(f"{sha16}*.json")):
        if not p.name.endswith(".meta.json"):
            return p
    return None


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def verify(root, eid: str) -> bool:
    """内容、meta、id 三者一致才算数；文件缺失或损坏返回 False。"""
    if not eid.startswith(_ID_PREFIX):
        return False
    sha16 = eid[len(_ID_PREFIX):]
    content_path = _find_content(Path(root), sha16)
    if content_path is None:
        return False
    meta_path = content_path.with_name(content_path.name[:-len(".json")]
                                       + ".meta.json")
    try:
        raw = content_path.read_bytes()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return meta.get("id") == eid and meta.get("sha") == _dig(raw)


def get(root, eid: str) -> dict:
    """按 id 读证据内容（JSON）。缺失抛 SecptError，不返回 None 让调用方猜。"""
    sha16 = eid[len(_ID_PREFIX):] if eid.startswith(_ID_PREFIX) else eid
    content_path = _find_content(Path(root), sha16)
    if content_path is None:
        from pa.errors import SecptError
        raise SecptError(f"evidence not found: {eid}")
    return json.loads(content_path.read_bytes())


def put(root, content, task_id: str, extra: dict | None = None) -> str:
    """落一份证据，返回证据 id。同内容同 id（内容寻址），重复 put 不重写；
    已存在但校验不过（半写中断）时原子重写自愈。"""
    if isinstance(content, str):
        content = content.encode("utf-8")
    sha = _dig(content)
    eid = _ID_PREFIX + sha[:16]
    if _find_content(Path(root), sha[:16]) is not None and verify(root, eid):
        return eid
    d = _dir(Path(root), sha)
    d.mkdir(parents=True, exist_ok=True)
    meta = {"id": eid, "sha": sha, "task_id": task_id, "ts": now().isoformat()}
    if extra:
        meta.update(extra)
    _atomic_write(d / f"{sha}.json", content)
    _atomic_write(d / f"{sha}.meta.json",
                  json.dumps(meta, ensure_ascii=False).encode("utf-8"))
    return eid
