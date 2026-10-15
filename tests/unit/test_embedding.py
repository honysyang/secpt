"""B 语义检索单测：嵌入缓存/降级/同义召回（假嵌入，可控向量，零推理成本）。"""

from __future__ import annotations

from secpt.knowledge import embedding as emb
from secpt.learning.retrieve import search_experiences
from secpt.learning.store import ExperienceJournal, LearnedExperience


def _fake_embedder(mapping: dict, default: list[float]):
    calls: list[list[str]] = []

    def _fn(texts):
        calls.append(list(texts))
        return [mapping.get(t, default) for t in texts]

    _fn.calls = calls  # type: ignore[attr-defined]
    return _fn


def _synonym_embedder(texts):
    """可控语义：含 'lfi' 或 '文件读取' → 同向 [1,0]；其余 [0,1]。"""
    return [[1.0, 0.0] if ("lfi" in t.lower() or "文件读取" in t) else [0.0, 1.0] for t in texts]


class TestEmbeddingModule:
    def setup_method(self):
        emb.reset_for_tests()

    def teardown_method(self):
        emb.reset_for_tests()

    def test_disabled_returns_none(self, monkeypatch):
        monkeypatch.setenv("SECPT_EMBED", "0")
        assert emb.embed_cached(["x"]) is None

    def test_injected_embedder_and_disk_cache(self, tmp_path, monkeypatch):
        """嵌入推理只发生一次：内存缓存 + 磁盘缓存（模拟进程重启后仍命中）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        fn = _fake_embedder({"a": [1.0, 0.0]}, [0.0, 1.0])
        emb.set_embedder(fn)
        v1 = emb.embed_cached(["a", "b"])
        assert v1 == [[1.0, 0.0], [0.0, 1.0]]
        assert emb.embed_cached(["a"]) == [[1.0, 0.0]]  # 命中缓存
        assert len(fn.calls) == 1
        # 模拟进程重启（清内存态）→ 从盘上恢复，仍零推理
        emb.reset_for_tests()
        emb.set_embedder(fn)
        assert emb.embed_cached(["a", "b"]) == v1
        assert len(fn.calls) == 1

    def test_provider_failure_degrades(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))

        def _boom(texts):
            raise RuntimeError("model crashed")

        emb.set_embedder(_boom)
        assert emb.embed_cached(["x"]) is None  # 静默降级，不抛

    def test_corrupt_cache_rebuilt(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        cache_file = tmp_path / "knowledge" / "embed-cache.json"
        cache_file.parent.mkdir(parents=True)
        cache_file.write_text("{broken", encoding="utf-8")
        fn = _fake_embedder({"a": [1.0, 0.0]}, [0.0, 1.0])
        emb.set_embedder(fn)
        assert emb.embed_cached(["a"]) == [[1.0, 0.0]]  # 损坏缓存丢弃重建

    def test_cosine(self):
        assert abs(emb.cosine([1, 0], [1, 0]) - 1.0) < 1e-6
        assert abs(emb.cosine([1, 0], [0, 1])) < 1e-6
        assert emb.cosine([0, 0], [1, 0]) == 0.0  # 零向量
        assert emb.cosine([1], [1, 2]) == 0.0  # 维度不一致


class TestSemanticRetrieve:
    def setup_method(self):
        emb.reset_for_tests()

    def teardown_method(self):
        emb.reset_for_tests()

    def test_synonym_recall(self, tmp_path, monkeypatch):
        """同义改写召回：“任意文件读取” vs 指标 “lfi”（字面全不命中 → 语义命中）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        jr = ExperienceJournal(tmp_path / "exp.jsonl")
        jr.append(LearnedExperience(title="LFI 读文件", technique="lfi", indicators=["lfi"]))
        q = "任意文件读取"
        assert search_experiences(q, journal=jr) == []  # 无嵌入：字面不召回
        emb.set_embedder(_synonym_embedder)
        hits = search_experiences(q, journal=jr)
        assert hits and hits[0].technique == "lfi"  # 语义通路召回

    def test_semantic_is_weak_signal_for_cross_domain_gate(self, tmp_path, monkeypatch):
        """语义命中属弱信号：跨任务类型仍不注入（宁缺毋滥的门槛不放宽）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        jr = ExperienceJournal(tmp_path / "exp.jsonl")
        jr.append(
            LearnedExperience(
                title="LFI 读文件", technique="lfi", indicators=["lfi"], task_type="ctf"
            )
        )
        emb.set_embedder(_synonym_embedder)
        assert search_experiences("任意文件读取", journal=jr, task_type="benchmark") == []
        hits = search_experiences("任意文件读取", journal=jr, task_type="ctf")
        assert hits and hits[0].technique == "lfi"

    def test_disabled_embed_keeps_keyword_path(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("SECPT_EMBED", "0")
        jr = ExperienceJournal(tmp_path / "exp.jsonl")
        jr.append(LearnedExperience(title="SAML XSW 绕过签名", technique="saml_xsw"))
        hits = search_experiences("saml 单点登录", journal=jr)
        assert hits and hits[0].technique == "saml_xsw"  # 字面路径行为不变


class TestIntelSemanticNeighbors:
    def setup_method(self):
        emb.reset_for_tests()

    def teardown_method(self):
        emb.reset_for_tests()

    def test_semantic_neighbor_cold_start(self, tmp_path, monkeypatch):
        """字面零命中时语义近邻冷启动召回（“命令执行”捞到“RCE”）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        from secpt.intel.store import IntelStore

        st = IntelStore(tmp_path / "intel.sqlite")
        st.add_node("fact", "目标存在命令注入点 cmd inj")
        st.add_node("fact", "RCE 可执行系统命令")
        st.add_node("fact", "无关事实 天气晴朗")

        def _inj_embedder(texts):
            def _hot(t: str) -> bool:
                return ("命令" in t) or ("inj" in t.lower() or "inject" in t.lower())

            return [[1.0, 0.0] if _hot(t) else [0.0, 1.0] for t in texts]

        emb.set_embedder(_inj_embedder)
        out = st.recall_graph("command injection")
        contents = [str(n.get("content") or "") for n in out]
        assert any("命令" in c for c in contents)
        assert any("RCE" in c for c in contents)
        assert all("天气" not in c for c in contents)  # 低于阈值的不召回

    def test_disabled_embed_falls_back(self, tmp_path, monkeypatch):
        """SECPT_EMBED=0：联想只走字面+血缘（与旧版行为一致）。"""
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        monkeypatch.setenv("SECPT_EMBED", "0")
        from secpt.intel.store import IntelStore

        st = IntelStore(tmp_path / "intel.sqlite")
        st.add_node("fact", "RCE 可执行系统命令")
        assert st.recall_graph("command injection") == []
