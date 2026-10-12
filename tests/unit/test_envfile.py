""".env 文件加载测试：注入、优先级、格式容错。"""

from __future__ import annotations

import os

from secpt.config.envfile import _LOADED, load_env_file


class TestLoadEnvFile:
    def _mk(self, tmp_path, text):
        p = tmp_path / ".env"
        p.write_text(text, encoding="utf-8")
        return p

    def test_injects_keys(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SECPT_TEST_A", raising=False)
        p = self._mk(tmp_path, "SECPT_TEST_A=hello\n")
        injected = load_env_file(p)
        assert injected == ["SECPT_TEST_A"]
        assert os.environ["SECPT_TEST_A"] == "hello"

    def test_real_env_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_TEST_B", "from-shell")
        p = self._mk(tmp_path, "SECPT_TEST_B=from-file\n")
        load_env_file(p)
        assert os.environ["SECPT_TEST_B"] == "from-shell"  # 真实环境优先

    def test_comments_blank_quotes(self, tmp_path, monkeypatch):
        for k in ("SECPT_TEST_C", "SECPT_TEST_D"):
            monkeypatch.delenv(k, raising=False)
            _LOADED.discard(k)
        p = self._mk(
            tmp_path,
            """
# 注释行
SECPT_TEST_C="quoted value"

SECPT_TEST_D='single'
""",
        )
        injected = load_env_file(p)
        assert set(injected) == {"SECPT_TEST_C", "SECPT_TEST_D"}
        assert os.environ["SECPT_TEST_C"] == "quoted value"
        assert os.environ["SECPT_TEST_D"] == "single"

    def test_missing_file_noop(self, tmp_path):
        assert load_env_file(tmp_path / "nope.env") == []

    def test_no_double_inject(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SECPT_TEST_E", raising=False)
        _LOADED.discard("SECPT_TEST_E")
        p = self._mk(tmp_path, "SECPT_TEST_E=1\n")
        assert load_env_file(p) == ["SECPT_TEST_E"]
        # 改写文件再加载：已加载的 key 不覆盖
        p.write_text("SECPT_TEST_E=2\n", encoding="utf-8")
        assert load_env_file(p) == []
        assert os.environ["SECPT_TEST_E"] == "1"

    def test_resolve_model_uses_env_key(self, tmp_path, monkeypatch):
        """端到端：.env 里的 DEEPSEEK_API_KEY 被模型路由读到。"""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        _LOADED.discard("DEEPSEEK_API_KEY")
        p = self._mk(tmp_path, "DEEPSEEK_API_KEY=sk-test-from-dotenv\n")
        load_env_file(p)
        from secpt.runtime.llm.client import resolve_model

        _base, key, _mid = resolve_model("deepseek-chat")
        assert key == "sk-test-from-dotenv"
