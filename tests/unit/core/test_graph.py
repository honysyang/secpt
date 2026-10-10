"""图存储测试：抽象、内存实现、runner 集成。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from secpt.config.models import GraphConfig
from secpt.core.graph import (
    MemoryGraphStore,
    get_graph_store,
)
from secpt.core.llm import FakeLLM, LLMMessage
from secpt.core.react.runner import run_react
from secpt.core.tools.base import Registry
from secpt.core.tools.demo import EchoTool
from secpt.runtime import subprocess as sp

# ---- MemoryGraphStore 基础 ----


@pytest.mark.asyncio
async def test_upsert_entity() -> None:
    store = MemoryGraphStore()
    await store.upsert_entity("host.1", "host", {"ip": "127.0.0.1"})
    assert store.get_entity("host.1") is not None
    assert store.get_entity("host.1")["etype"] == "host"
    assert store.get_entity("host.1")["props"]["ip"] == "127.0.0.1"


@pytest.mark.asyncio
async def test_add_edge_and_neighbors() -> None:
    store = MemoryGraphStore()
    await store.upsert_entity("a", "task", {})
    await store.upsert_entity("b", "action", {})
    await store.add_edge("a", "b", "performed", confidence=1.0)
    neighbors = await store.neighbors("a")
    assert len(neighbors) == 1
    assert neighbors[0]["eid"] == "b"
    assert neighbors[0]["edge_type"] == "performed"
    assert neighbors[0]["confidence"] == 1.0


@pytest.mark.asyncio
async def test_add_evidence_returns_id() -> None:
    store = MemoryGraphStore()
    eid = await store.add_evidence("host.1", "nmap: 22/tcp open", trust_tier="deterministic")
    assert eid
    ev = store.get_evidence(eid)
    assert ev is not None
    assert ev["payload"] == "nmap: 22/tcp open"
    assert ev["trust_tier"] == "deterministic"
    assert ev["trust_score"] == 1.0


@pytest.mark.asyncio
async def test_invalid_trust_tier_rejected() -> None:
    """pydantic ValidationError：非法 trust_tier 必须被拦。"""
    from secpt.core.graph.base import EvidenceRecord

    with pytest.raises(ValidationError):
        EvidenceRecord(
            evidence_id="ev.x",
            eid="e.1",
            payload="x",
            trust_tier="bogus",  # type: ignore[arg-type]
            trust_score=0.5,
        )


@pytest.mark.asyncio
async def test_factory_memory_backend() -> None:
    cfg = GraphConfig(backend="memory")
    store = get_graph_store(cfg)
    assert isinstance(store, MemoryGraphStore)


@pytest.mark.asyncio
async def test_factory_unsupported_backend() -> None:
    """postgres 后端无 dsn 时应抛 ValueError。"""
    cfg = GraphConfig(backend="postgres", dsn="")
    with pytest.raises(ValueError, match="dsn"):
        get_graph_store(cfg)


# ---- runner 集成 ----


@pytest.fixture
def echo_registry(monkeypatch: pytest.MonkeyPatch) -> Registry:
    from secpt.config.models import ToolsConfig

    cfg = ToolsConfig(whitelist=["echo"])
    fake_settings = type("S", (), {"tools": cfg})()
    monkeypatch.setattr(sp, "get_settings", lambda: fake_settings)

    reg = Registry()
    reg.register(EchoTool())
    return reg


@pytest.mark.asyncio
async def test_runner_with_store_writes_graph(echo_registry: Registry) -> None:
    """runner 接 store：action 实体、performed 边、evidence 都落图。"""
    llm = FakeLLM(
        script=[
            LLMMessage(role="assistant", content="ACTION: echo hello"),
            LLMMessage(role="assistant", content="ANSWER: X"),
        ]
    )
    store = MemoryGraphStore()
    result = await run_react(
        llm=llm,
        tools=echo_registry,
        task="test task",
        store=store,
    )
    assert result.answer == "X"
    assert result.exhausted is False

    # action 实体存在
    assert store.get_entity("act.1.echo") is not None
    assert store.get_entity("act.1.echo")["props"]["tool"] == "echo"

    # performed 边存在（task 根 → action）
    import hashlib

    task_hash = hashlib.sha256(b"test task").hexdigest()[:12]
    task_eid = f"task.{task_hash}"
    assert store.has_edge(task_eid, "act.1.echo", "performed") is True

    # evidence 存在且 trust_tier 正确
    neighbors = await store.neighbors(task_eid)
    action_nbs = [n for n in neighbors if n["eid"] == "act.1.echo"]
    assert len(action_nbs) == 1


@pytest.mark.asyncio
async def test_runner_dedup_action(echo_registry: Registry) -> None:
    """同 tool+args 第二次调用应被拦截，不重复执行。"""
    llm = FakeLLM(
        script=[
            LLMMessage(role="assistant", content="ACTION: echo same"),
            LLMMessage(role="assistant", content="ACTION: echo same"),
            LLMMessage(role="assistant", content="ANSWER: done"),
        ]
    )
    store = MemoryGraphStore()
    result = await run_react(
        llm=llm,
        tools=echo_registry,
        task="dedup test",
        store=store,
        max_steps=5,
    )
    assert result.exhausted is False
    assert result.answer == "done"
    # 第 2 步应被去重
    dedup_steps = [t for t in result.trajectory if t.get("deduplicated")]
    assert len(dedup_steps) == 1
    assert "已执行过" in dedup_steps[0]["observation"]


@pytest.mark.asyncio
async def test_runner_without_store_no_regression(echo_registry: Registry) -> None:
    """不传 store：行为与 Step 3 完全一致。"""
    llm = FakeLLM(
        script=[
            LLMMessage(role="assistant", content="ACTION: echo hello"),
            LLMMessage(role="assistant", content="ANSWER: X"),
        ]
    )
    result = await run_react(llm=llm, tools=echo_registry, task="test task")
    assert result.answer == "X"
    assert result.steps == 2
    assert result.exhausted is False
    assert result.trajectory == [
        {"type": "action", "step": 1, "tool": "echo", "args": "hello", "observation": "hello"},
        {"type": "finish", "step": 2, "answer": "X"},
    ]
