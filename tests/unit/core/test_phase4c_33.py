"""4c Postgres 后端 + 3.3 知识库测试。"""

from __future__ import annotations

import os

import pytest

from secpt.config.models import GraphConfig
from secpt.core.graph import get_graph_store
from secpt.knowledge import Experience, ExperienceStore


# ---- 4c Postgres ----

HAS_PG = bool(os.environ.get("SECPT_TEST_PG_DSN"))


@pytest.mark.skipif(not HAS_PG, reason="需要 SECPT_TEST_PG_DSN 环境变量")
@pytest.mark.asyncio
async def test_postgres_graph_store() -> None:
    """真实 Postgres 测试，需要 SECPT_TEST_PG_DSN。"""
    dsn = os.environ["SECPT_TEST_PG_DSN"]
    cfg = GraphConfig(backend="postgres", dsn=dsn)
    store = get_graph_store(cfg)

    # 写入
    await store.upsert_entity("host.pg1", "host", {"ip": "10.0.0.99"})
    eid = await store.add_evidence("host.pg1", "nmap result", trust_tier="deterministic")
    assert eid.startswith("ev.")
    await store.add_edge("task.1", "host.pg1", "performed")

    # 查询
    neighbors = await store.neighbors("task.1")
    assert len(neighbors) == 1
    assert neighbors[0]["eid"] == "host.pg1"

    await store.close()


def test_postgres_factory_no_dsn() -> None:
    """无 DSN 时应抛 ValueError。"""
    cfg = GraphConfig(backend="postgres", dsn="")
    with pytest.raises(ValueError, match="dsn"):
        get_graph_store(cfg)


def test_factory_unsupported_backend() -> None:
    """未知后端在 pydantic Literal 层就被拦下（治理在配置层生效）。"""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        GraphConfig(backend="unknown_backend")  # type: ignore[arg-type]


# ---- 3.3 知识库 ----


def test_experience_store_add_and_get() -> None:
    store = ExperienceStore()
    exp = Experience(
        id="exp.1",
        title="Apache Struts RCE",
        description="CVE-2017-5638 Apache Struts2 远程代码执行",
        technique="web_rce",
        indicators=["struts", "ognl", "rce"],
        severity="critical",
    )
    store.add(exp)
    retrieved = store.get("exp.1")
    assert retrieved is not None
    assert retrieved.title == "Apache Struts RCE"
    assert "rce" in retrieved.indicators


def test_experience_store_search() -> None:
    store = ExperienceStore()
    store.add(Experience(
        id="exp.1",
        title="SSH brute force",
        description="hydra 爆破 SSH 弱密码",
        technique="credential_attack",
        indicators=["ssh", "brute", "hydra"],
    ))
    store.add(Experience(
        id="exp.2",
        title="SQL injection",
        description="union-based SQLi",
        technique="web_injection",
        indicators=["sqli", "union", "injection"],
    ))
    results = store.search("ssh brute")
    assert len(results) >= 1
    assert any("SSH" in r.title for r in results)


def test_experience_store_save_and_load(tmp_path) -> None:
    path = str(tmp_path / "exp.json")
    store1 = ExperienceStore(path)
    store1.add(Experience(
        id="exp.1",
        title="Test",
        description="Test description",
        technique="test",
        indicators=["test"],
    ))
    store1.save()

    store2 = ExperienceStore(path)
    assert len(store2.all()) == 1
    assert store2.get("exp.1") is not None


def test_experience_store_match_for_degradation() -> None:
    """模拟机制③：经验匹配。"""
    store = ExperienceStore()
    store.add(Experience(
        id="exp.1",
        title="WordPress fingerprint",
        description="whatweb 识别 WordPress 指纹后常见插件漏洞",
        technique="web_fingerprint",
        indicators=["wordpress", "wp-content", "plugin"],
    ))
    matches = store.match("found wordpress site with wp-content directory")
    assert len(matches) >= 1
    assert "WordPress" in matches[0].title
