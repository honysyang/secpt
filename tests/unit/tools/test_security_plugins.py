"""安全插件单测：YAML 模板装配 / analyze_binary / crypto_helpers 门控。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from secpt.tools.plugin_loader import load_yaml_plugin

PLUGINS_DIR = Path(__file__).resolve().parents[3] / "tools" / "plugins"

_HAS_OBJDUMP = shutil.which("objdump") is not None
_HAS_OPENSSL = shutil.which("openssl") is not None

# Python 插件不在包路径上，按 plugin_loader 同款方式注入目录后直接 import
import sys  # noqa: E402

sys.path.insert(0, str(PLUGINS_DIR))


# ---------------- YAML 模板插件 ----------------


def test_pwn_yaml_plugin_valid() -> None:
    p = load_yaml_plugin(PLUGINS_DIR / "pwn.yaml")
    assert p.name == "pwn_checksec"
    assert "{binary}" in p.command
    assert "binary" in p.schema["properties"]
    assert set(p.schema["required"]) <= set(p.schema["properties"])


def test_crypto_yaml_plugin_valid() -> None:
    p = load_yaml_plugin(PLUGINS_DIR / "crypto.yaml")
    assert p.name == "crypto_pubkey"
    assert "-pubin" in p.command  # 只支持公钥材料
    assert "PRIVATE" not in p.command


# ---------------- analyze_binary ----------------


def test_analyze_binary_rejects_missing_path() -> None:
    import asyncio

    from analyze_binary import _fn

    out = asyncio.run(_fn({"path": "/nonexistent-bin-xyz"}))
    assert out.startswith("Error:") and "文件" in out


@pytest.mark.skipif(not _HAS_OBJDUMP, reason="objdump 未安装")
def test_analyze_binary_real_run(tmp_path: Path, monkeypatch) -> None:
    """对真实 ELF 跑全链路：JSON 摘要 + 完整结果落 outputs/。"""
    import asyncio

    monkeypatch.setenv("SECPT_WORK_DIR", str(tmp_path))
    from analyze_binary import _fn

    out = asyncio.run(_fn({"path": "/bin/true"}))
    data = json.loads(out)
    assert data["path"] == "/bin/true"
    assert any(s["name"] == ".text" for s in data["sections"])  # objdump -h 真实产出
    assert Path(data["full_json"]).exists()  # 完整 JSON 落盘
    full = json.loads(Path(data["full_json"]).read_text(encoding="utf-8"))
    assert set(full) >= {"sections", "symbols", "strings", "rop_gadgets", "missing_tools"}


# ---------------- crypto_helpers（admin enable 门控） ----------------


def test_crypto_helpers_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("SECPT_ENABLE_CRYPTO_HELPERS", raising=False)
    import importlib.util

    spec = importlib.util.spec_from_file_location("ch_disabled", PLUGINS_DIR / "crypto_helpers.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.register() == {}  # 未 admin enable → 不注册任何工具


def test_crypto_helpers_enabled(monkeypatch) -> None:
    monkeypatch.setenv("SECPT_ENABLE_CRYPTO_HELPERS", "1")
    import importlib.util

    spec = importlib.util.spec_from_file_location("ch_enabled", PLUGINS_DIR / "crypto_helpers.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tools = mod.register()
    assert set(tools) == {"parse_cert", "pubkey_info", "rsa_modexp", "hash_compare"}

    # rsa_modexp：纯计算正确性（10^3 mod 17 = 14；负指数按契约返回 Error）
    out = json.loads(tools["rsa_modexp"][1]({"base": "10", "exp": "3", "mod": "17"}))
    assert out["dec"] == "14"
    out = tools["rsa_modexp"][1]({"base": "10", "exp": "-1", "mod": "17"})
    assert "Error" in out  # 负指数拒绝（模逆元请走 gmpy2 脚本）
    out = json.loads(tools["rsa_modexp"][1]({"base": "0x10", "exp": "0x2", "mod": "100"}))
    assert out["dec"] == str(pow(16, 2, 100))

    # hash_compare：归一化 + 常量时间
    out = json.loads(tools["hash_compare"][1]({"a": "0xABCDEF", "b": "ab:cd:ef"}))
    assert out["equal"] is True
    out = json.loads(tools["hash_compare"][1]({"a": "aa", "b": "bb"}))
    assert out["equal"] is False


@pytest.mark.skipif(not _HAS_OPENSSL, reason="openssl 未安装")
def test_pubkey_info_rejects_private_key(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SECPT_ENABLE_CRYPTO_HELPERS", "1")
    import importlib.util

    spec = importlib.util.spec_from_file_location("ch_pk", PLUGINS_DIR / "crypto_helpers.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tools = mod.register()
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA1234\n-----END RSA PRIVATE KEY-----\n"
    # pem 入参直接拒绝（不落 openssl）
    out = tools["pubkey_info"][1]({"pem": pem})
    assert "拒绝" in out and "私钥" in out
    # 公钥正常解析
    pub = tmp_path / "pub.pem"
    pub.write_text(
        "-----BEGIN PUBLIC KEY-----\nMFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBAK\n-----END PUBLIC KEY-----\n",
        encoding="utf-8",
    )
    out = tools["pubkey_info"][1]({"path": str(pub)})
    assert "Error:" not in out or "不存在" not in out
