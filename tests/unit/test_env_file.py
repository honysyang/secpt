"""M5-20 `.env` 装载测试：解析 / 白名单 / 优先级 / 显式路径 / CLI 接线。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from secpt import main as main_mod
from secpt.env_file import apply_env_file, bootstrap_env, load_env_file, parse_env_text
from secpt.errors import SecptError


def _write(tmp_path: Path, text: str, name: str = ".env") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestParse:
    def test_supported_forms(self) -> None:
        text = "# 注释\n\nA=1\nexport B=2\nC = 'x y' \nD=\"z\"\n坏行没有等号\nE=\n"
        assert parse_env_text(text) == {"A": "1", "B": "2", "C": "x y", "D": "z", "E": ""}

    def test_invalid_key_skipped(self) -> None:
        assert parse_env_text("1A=x\nA B=y\n") == {}


class TestWhitelist:
    def test_only_secpt_and_provider(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "SECPT_DB=a.db\nLLM_PROVIDER=openai\nPATH=/evil\nMEMORY_BACKEND=memory\n",
        )
        assert load_env_file(path) == {"SECPT_DB": "a.db", "LLM_PROVIDER": "openai"}

    def test_missing_file_is_e_config(self, tmp_path: Path) -> None:
        with pytest.raises(SecptError) as info:
            load_env_file(tmp_path / "no.env")
        assert info.value.code == "E_CONFIG"


class TestApply:
    def test_real_env_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = _write(tmp_path, "SECPT_DB=from-file\nSECPT_ENV=from-file\n")
        monkeypatch.setenv("SECPT_DB", "from-env")
        monkeypatch.delenv("SECPT_ENV", raising=False)
        applied = apply_env_file(path)
        try:
            assert applied == ["SECPT_ENV"]
            assert os.environ["SECPT_DB"] == "from-env"  # 真实环境优先
            assert os.environ["SECPT_ENV"] == "from-file"
        finally:
            os.environ.pop("SECPT_ENV", None)  # apply 直接写 os.environ，手动回收


class TestBootstrap:
    def test_no_var_and_no_file_means_no_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SECPT_ENV_FILE", raising=False)
        monkeypatch.setenv("SECPT_DOTENV", "0")
        assert bootstrap_env() == []

    def test_auto_loads_cwd_dotenv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """M5-21：未显式指路时自动读当前目录 .env（用户要求）。"""
        _write(tmp_path, "SECPT_ENV=auto\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SECPT_ENV_FILE", raising=False)
        monkeypatch.delenv("SECPT_DOTENV", raising=False)
        monkeypatch.delenv("SECPT_ENV", raising=False)
        try:
            assert bootstrap_env() == ["SECPT_ENV"]
            assert os.environ["SECPT_ENV"] == "auto"
        finally:
            os.environ.pop("SECPT_ENV", None)

    def test_toggle_off_disables_auto_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(tmp_path, "SECPT_ENV=x\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SECPT_ENV_FILE", raising=False)
        monkeypatch.setenv("SECPT_DOTENV", "0")
        assert bootstrap_env() == []

    def test_explicit_path_beats_toggle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """显式 SECPT_ENV_FILE 不受开关影响（显式意图优先）。"""
        path = _write(tmp_path, "SECPT_ENV=explicit\n")
        monkeypatch.setenv("SECPT_ENV_FILE", str(path))
        monkeypatch.setenv("SECPT_DOTENV", "0")
        monkeypatch.delenv("SECPT_ENV", raising=False)
        try:
            assert bootstrap_env() == ["SECPT_ENV"]
        finally:
            os.environ.pop("SECPT_ENV", None)

    def test_cli_bootstraps_before_dispatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() 入口装载：命令执行时环境里已有文件里的值。"""
        path = _write(tmp_path, "SECPT_ENV=boot\n")
        monkeypatch.setenv("SECPT_ENV_FILE", str(path))
        monkeypatch.delenv("SECPT_ENV", raising=False)
        try:
            assert main_mod.main(["assets"]) == 0
            assert os.environ["SECPT_ENV"] == "boot"
        finally:
            os.environ.pop("SECPT_ENV", None)

    def test_bad_path_rejects_with_e_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SECPT_ENV_FILE", str(tmp_path / "no.env"))
        assert main_mod.main(["assets"]) == 1
