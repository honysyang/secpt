"""EvidenceStore 单测：blob CRUD / 去重 / 绑定 / 导出（纯 SQLite，无网络）。"""

from __future__ import annotations

import json

import pytest

from secpt.evidence.store import (
    EXCHANGE_FORMAT,
    EvidenceStore,
    serialize_exchange,
    try_parse_exchange,
)
from secpt.traffic.store import TrafficStore


@pytest.fixture
def ev(tmp_path):
    store = EvidenceStore(root=tmp_path / "evidence")
    yield store
    store.close()


@pytest.fixture
def traffic(tmp_path):
    store = TrafficStore(tmp_path / "traffic" / "job9.sqlite")
    yield store
    store.close()


def _flow(ex_id: int = 1) -> dict:
    return {
        "id": ex_id,
        "method": "POST",
        "url": "http://api.test/login?next=/admin",
        "status": 302,
        "req_headers": {"Host": "api.test", "Content-Type": "application/x-www-form-urlencoded"},
        "req_body": b"user=admin&pass=hunter2",
        "resp_headers": {"Location": "/admin", "Set-Cookie": "sid=xyz"},
        "resp_body": b"",
        "ts": 1700000123.0,
    }


def _record(traffic: TrafficStore, **overrides: object) -> int:
    flow = _flow()
    flow.pop("id")
    flow.update(overrides)
    ex_id = traffic.record(flow)
    assert ex_id is not None
    return ex_id


# ── blob 层 ────────────────────────────────────────────────


def test_store_blob_content_addressed(ev):
    h1 = ev.store_blob(b"hello-evidence")
    assert len(h1) == 64
    assert ev.get_blob(h1) == b"hello-evidence"
    h2 = ev.store_blob(b"hello-evidence")
    assert h1 == h2  # 内容寻址：同内容同 hash


def test_store_blob_dedup_file_written_once(ev, monkeypatch):
    writes = []
    original = type(ev).store_blob

    def counting_write(self, data):
        writes.append(1)
        return original(self, data)

    monkeypatch.setattr(EvidenceStore, "_blob_write_marker", None, raising=False)
    h = ev.store_blob(b"dup")
    mtime = ev._blob_path(h).stat().st_mtime_ns
    import time

    time.sleep(0.01)
    h2 = ev.store_blob(b"dup")
    assert h2 == h
    assert ev._blob_path(h).stat().st_mtime_ns == mtime  # 未重写


def test_blob_path_sharding(ev):
    h = ev.store_blob(b"shard-me")
    assert ev._blob_path(h).parent == ev.blobs_dir / h[:2]
    assert ev._blob_path(h).name == f"{h}.bin"


def test_get_blob_missing(ev):
    assert ev.get_blob("0" * 64) is None
    assert ev.has_blob("0" * 64) is False


# ── 绑定层 ─────────────────────────────────────────────────


def test_attach_and_list(ev):
    h = ev.store_blob(b"proof-bytes")
    bid = ev.attach("FND-1", h, "proof", exchange_id=7, note="登录响应")
    assert bid == 1
    rows = ev.list_for("FND-1")
    assert len(rows) == 1
    row = rows[0]
    assert row["blob_hash"] == h
    assert row["role"] == "proof"
    assert row["exchange_id"] == 7
    assert row["note"] == "登录响应"
    assert row["size"] == len(b"proof-bytes")
    assert row["finding_id"] == "FND-1"


def test_attach_all_roles(ev):
    for role in ("baseline", "proof", "verification", "supporting"):
        h = ev.store_blob(role.encode())
        assert ev.attach("FND-R", h, role) is not None
    assert len(ev.list_for("FND-R")) == 4


def test_attach_invalid_role_raises(ev):
    h = ev.store_blob(b"x")
    with pytest.raises(ValueError, match="非法证据角色"):
        ev.attach("FND-1", h, "bogus")


def test_attach_missing_blob_returns_none(ev, capsys):
    assert ev.attach("FND-1", "f" * 64, "proof") is None
    assert "blob 不存在" in capsys.readouterr().out


def test_list_for_empty(ev):
    assert ev.list_for("NOPE") == []


# ── 快照 ───────────────────────────────────────────────────


def test_snapshot_from_exchange(ev, traffic):
    ex_id = _record(traffic)
    h = ev.snapshot_from_exchange(traffic, ex_id)
    assert h is not None
    blob = ev.get_blob(h)
    envelope = try_parse_exchange(blob)
    assert envelope is not None
    assert envelope["format"] == EXCHANGE_FORMAT
    assert envelope["method"] == "POST"
    assert envelope["url"] == "http://api.test/login?next=/admin"
    assert envelope["status"] == 302
    assert envelope["request"]["headers"]["Host"] == "api.test"


def test_snapshot_missing_exchange(ev, traffic, capsys):
    assert ev.snapshot_from_exchange(traffic, 999) is None
    assert "不存在" in capsys.readouterr().out


def test_serialize_exchange_roundtrip():
    env = try_parse_exchange(serialize_exchange(_flow()))
    assert env is not None
    assert env["version"] == 1
    assert env["request"]["body_b64"]  # base64 存在


def test_try_parse_exchange_rejects_non_exchange():
    assert try_parse_exchange(b"not json") is None
    assert try_parse_exchange(json.dumps({"format": "other"}).encode()) is None


# ── 导出 ───────────────────────────────────────────────────


def test_export_finding_structure(ev, traffic, tmp_path):
    ex_id = _record(traffic)
    h = ev.snapshot_from_exchange(traffic, ex_id)
    ev.attach("FND-X", h, "proof", exchange_id=ex_id, note="越权登录证据")
    raw_h = ev.store_blob(b"\x00\x01raw-bytes")  # 非 exchange blob
    ev.attach("FND-X", raw_h, "supporting")

    dest = ev.export_finding("FND-X", tmp_path / "out")
    assert dest == tmp_path / "out" / "FND-X"
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["finding_id"] == "FND-X"
    assert manifest["count"] == 2
    assert len(manifest["items"]) == 2

    proof_item = next(i for i in manifest["items"] if i["role"] == "proof")
    assert proof_item["blob_hash"] == h
    assert proof_item["exchange_id"] == ex_id
    assert proof_item["note"] == "越权登录证据"
    assert set(proof_item["files"]) == {
        f"{proof_item['binding_id']:04d}_proof_request.http",
        f"{proof_item['binding_id']:04d}_proof_response.http",
    }
    for name in proof_item["files"]:
        assert (dest / name).exists()

    # request.http 风格：首行 METHOD path HTTP/1.1，body 还原
    req_file = dest / proof_item["files"][0]
    content = req_file.read_bytes()
    assert content.startswith(b"POST /login?next=/admin HTTP/1.1\r\n")
    assert b"Host: api.test\r\n" in content
    assert content.endswith(b"user=admin&pass=hunter2")

    raw_item = next(i for i in manifest["items"] if i["role"] == "supporting")
    assert raw_item["files"][0].endswith(".bin")
    assert (dest / raw_item["files"][0]).read_bytes() == b"\x00\x01raw-bytes"


def test_export_finding_empty(ev, tmp_path):
    dest = ev.export_finding("FND-EMPTY", tmp_path / "out")
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["count"] == 0
    assert manifest["items"] == []


def test_export_job(ev, traffic, tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path / "var"))
    _record(traffic)
    _record(traffic, method="GET", url="http://api.test/admin", status=200)
    # traffic fixture 的库路径在 tmp/var/traffic 之外，导出按 SECPT_VAR 找库；
    # 复制一份到约定位置
    import shutil

    target = tmp_path / "var" / "traffic" / "job9.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(traffic.db_path, target)

    dest = ev.export_job("job9", tmp_path / "jobout")
    assert dest is not None and dest.name == "job9"
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["job_id"] == "job9"
    assert manifest["count"] == 2
    assert all(len(i["files"]) == 2 for i in manifest["items"])
    assert (dest / "exchange_0001_request.http").exists()
    assert (dest / "exchange_0002_response.http").exists()


def test_export_job_missing(ev, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path / "var"))
    assert ev.export_job("no-such-job", tmp_path / "out") is None
    assert "无流量库" in capsys.readouterr().out
