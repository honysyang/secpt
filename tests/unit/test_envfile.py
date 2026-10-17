""".env 文件加载测试：注入、优先级、格式容错。"""

from __future__ import annotations

import os

from secpt.config.envfile import _LOADED, load_env_file, parse_env_file


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

    def test_export_prefix(self, tmp_path, monkeypatch):
        """``export KEY=VALUE`` 前缀（从 shell 片段/其他项目复制的常见写法）。"""
        for k in ("SECPT_TEST_EXPORT",):
            monkeypatch.delenv(k, raising=False)
            _LOADED.discard(k)
        p = self._mk(tmp_path, "export SECPT_TEST_EXPORT=hello\n")
        assert load_env_file(p) == ["SECPT_TEST_EXPORT"]
        assert os.environ["SECPT_TEST_EXPORT"] == "hello"

    def test_inline_comment_stripped(self, tmp_path, monkeypatch):
        """行内注释剥离：``#`` 前有空白才起注释（base URL 后随手写注释不再污染值）。"""
        for k in ("SECPT_TEST_URL", "SECPT_TEST_FRAG"):
            monkeypatch.delenv(k, raising=False)
            _LOADED.discard(k)
        p = self._mk(
            tmp_path,
            "SECPT_TEST_URL=https://gw.internal/v1  # 主网关\n"
            "SECPT_TEST_FRAG=https://x/page#anchor\n",
        )
        load_env_file(p)
        assert os.environ["SECPT_TEST_URL"] == "https://gw.internal/v1"
        # ``#`` 前无空白 → 是值的一部分（URL fragment / 颜色码等），不剥离
        assert os.environ["SECPT_TEST_FRAG"] == "https://x/page#anchor"

    def test_quoted_value_keeps_hash(self, tmp_path, monkeypatch):
        """引号内的 ``#`` 是值的一部分；引号后的内容视为注释丢弃。"""
        for k in ("SECPT_TEST_Q", "SECPT_TEST_Q2"):
            monkeypatch.delenv(k, raising=False)
            _LOADED.discard(k)
        p = self._mk(
            tmp_path,
            "SECPT_TEST_Q=\"a # b\"\nSECPT_TEST_Q2='single'  # trailing comment\n",
        )
        load_env_file(p)
        assert os.environ["SECPT_TEST_Q"] == "a # b"
        assert os.environ["SECPT_TEST_Q2"] == "single"

    def test_parse_env_file_readonly(self, tmp_path, monkeypatch):
        """parse_env_file 只解析不注入（doctor 等只读场景）。"""
        monkeypatch.delenv("SECPT_TEST_RO", raising=False)
        p = self._mk(tmp_path, "# c\nexport SECPT_TEST_RO=1  # note\n")
        assert parse_env_file(p) == {"SECPT_TEST_RO": "1"}
        assert "SECPT_TEST_RO" not in os.environ

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

    def test_resolve_model_base_url_from_dotenv(self, tmp_path, monkeypatch):
        """端到端：.env 里的 DEEPSEEK_BASE_URL 被模型路由读到（base URL 可从 .env 导入）。"""
        for k in (
            "DEEPSEEK_API_KEY",
            "DEEPSEEK_BASE_URL",
            "SECPT_MODEL_BASE_URL",
            "SECPT_MODEL_BASE_DEEPSEEK",
        ):
            monkeypatch.delenv(k, raising=False)
            _LOADED.discard(k)
        p = self._mk(
            tmp_path,
            "DEEPSEEK_API_KEY=sk-test-from-dotenv\n"
            "DEEPSEEK_BASE_URL=https://gw.from-dotenv/v1  # 网关\n",
        )
        load_env_file(p)
        from secpt.runtime.llm.client import resolve_model

        base, _key, _mid = resolve_model("deepseek-chat")
        assert base == "https://gw.from-dotenv/v1"


class TestModelEnvConfig:
    def test_primary_fallback_from_env(self, monkeypatch):
        monkeypatch.setenv("SECPT_MODEL_PRIMARY", "openai/gpt-4o")
        monkeypatch.setenv("SECPT_MODEL_FALLBACK", "mygw/qwen3-32b")
        from secpt.cli.engine_runner import resolve_models

        assert resolve_models(None, None) == ("openai/gpt-4o", "mygw/qwen3-32b")
        # 显式参数优先于环境变量
        assert resolve_models("deepseek-chat", None) == ("deepseek-chat", "mygw/qwen3-32b")

    def test_defaults_without_env(self, monkeypatch):
        monkeypatch.delenv("SECPT_MODEL_PRIMARY", raising=False)
        monkeypatch.delenv("SECPT_MODEL_FALLBACK", raising=False)
        from secpt.cli.engine_runner import resolve_models

        assert resolve_models(None, None) == ("deepseek-chat", "deepseek-reasoner")
