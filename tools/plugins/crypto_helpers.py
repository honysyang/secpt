"""crypto_helpers：常见密码学 helper 工具集（只读计算，admin enable 门控）。

提供四类无状态 helper（require admin enable）：
  parse_cert    — 解析 X.509 证书（openssl x509，只读）
  pubkey_info   — 查看公钥参数（openssl pkey -pubin，只支持公钥材料）
  rsa_modexp    — 模幂计算 pow(c, e, n)（纯 Python 大整数，无外部依赖）
  hash_compare  — 常量时间哈希/摘要比对（hmac.compare_digest）

安全边界（明确禁令）：
- 默认不注册：必须显式设置 SECPT_ENABLE_CRYPTO_HELPERS=1（admin enable）
- 只支持公钥/证书材料（-pubin）；明确禁止在默认 profile 加载私钥
- 引擎侧 keyguard 兜底：任何含 PEM 私钥块的工具输出都不会写入 transcript

用途：RSA 公钥参数提取、模幂/逆元快速验证、哈希比对等解题常用操作，
替代「模型手写大数脚本」的低效路径。
"""

from __future__ import annotations

import hmac
import os
import re
import subprocess
from pathlib import Path

ENABLE_ENV = "SECPT_ENABLE_CRYPTO_HELPERS"
OUT_CAP = 6000
_TIMEOUT = 30


def _enabled() -> bool:
    return os.environ.get(ENABLE_ENV, "") == "1"


def _run_openssl(argv: list[str], stdin_pem: str = "") -> str:
    """openssl 只读执行（禁 shell=True）；返回 stderr 一并给模型（含报错原因）。"""
    proc = subprocess.run(
        ["openssl", *argv],
        input=stdin_pem.encode() if stdin_pem else None,
        capture_output=True,
        timeout=_TIMEOUT,
    )
    return (proc.stdout + (b"\n" + proc.stderr if proc.stderr else b"")).decode(errors="replace")


def _material_path(args: dict, key: str) -> tuple[str, str]:
    """解析材料来源：优先 path（文件，只读打开），其次 pem（标准输入）。"""
    p = args.get(key) or ""
    if p:
        fp = Path(p)
        if not fp.is_file():
            return "", f"Error: {key} 文件不存在: {p}"
        return fp.read_text(encoding="utf-8", errors="replace"), ""
    pem = args.get("pem") or ""
    if pem and "PRIVATE KEY" in pem:
        return "", "Error: 拒绝处理私钥材料（仅支持证书/公钥）；keyguard 也会拦截私钥入档"
    return pem, ""


# ---------------- parse_cert ----------------


def parse_cert(path: str = "", pem: str = "") -> str:
    """解析 X.509 证书：subject/issuer/有效期/SAN/公钥算法。"""
    _material, err = _material_path({"path": path, "pem": pem}, "path")
    if err:
        return err
    argv = ["x509", "-noout", "-subject", "-issuer", "-dates", "-ext", "subjectAltName"]
    if path:
        argv += ["-in", path]
    raw = _run_openssl(argv, stdin_pem=(pem if not path else ""))
    info = _run_openssl(
        ["x509", "-noout", "-text"] + (["-in", path] if path else []),
        stdin_pem=(pem if not path else ""),
    )
    pubkey_alg = ""
    m = re.search(r"Public Key Algorithm:\s*(\S+)", info)
    if m:
        pubkey_alg = m.group(1)
    bits = re.search(r"(?:Public-Key|RSA Public-Key):\s*\((\d+) bit\)", info)
    payload = {"subject_issuer_dates_san": raw.strip(), "pubkey_alg": pubkey_alg}
    if bits:
        payload["pubkey_bits"] = int(bits.group(1))
    import json

    return json.dumps(payload, ensure_ascii=False)[:OUT_CAP]


# ---------------- pubkey_info ----------------


def pubkey_info(path: str = "", pem: str = "") -> str:
    """查看公钥参数（openssl pkey -pubin -text）。仅公钥材料。"""
    _material, err = _material_path({"path": path, "pem": pem}, "path")
    if err:
        return err
    if not path and pem and "PRIVATE KEY" in pem:
        return "Error: 拒绝处理私钥材料（-pubin 仅支持公钥）"
    argv = ["pkey", "-pubin", "-text", "-noout"]
    if path:
        argv += ["-in", path]
    raw = _run_openssl(argv, stdin_pem=(pem if not path else ""))
    return raw[:OUT_CAP] if raw.strip() else "(no output)"


# ---------------- rsa_modexp ----------------


def rsa_modexp(base: str, exp: str, mod: str) -> str:
    """模幂 pow(base, exp, mod)——入参支持十进制/0x 十六进制大整数字符串。"""
    import json

    try:
        b, e, m = int(base, 0), int(exp, 0), int(mod, 0)
    except ValueError as ex:
        return f"Error: 参数需为十进制或 0x 前缀十六进制整数: {ex}"
    if m <= 0 or e < 0:
        return "Error: mod 必须为正、exp 必须非负"
    val = pow(b, e, m)
    return json.dumps({"dec": str(val), "hex": hex(val)})


# ---------------- hash_compare ----------------


def hash_compare(a: str, b: str) -> str:
    """常量时间比对两个哈希/摘要（归一化大小写与 0x 前缀、冒号/空格分隔符）。"""
    import json

    def norm(x: str) -> str:
        x = (x or "").strip().lower().removeprefix("0x")
        return re.sub(r"[:\s]", "", x)

    na, nb = norm(a), norm(b)
    equal = hmac.compare_digest(na, nb)
    return json.dumps({"equal": equal, "len_a": len(na), "len_b": len(nb)})


# ---------------- 插件注册（admin enable 门控） ----------------

_TOOLS: dict[str, tuple[dict, object]] = {}


def _schema(desc: str, props: dict, required: list[str]) -> dict:
    return {
        "description": desc,
        "type": "object",
        "properties": props,
        "required": required,
    }


def _build() -> dict[str, tuple[dict, object]]:
    return {
        "parse_cert": (
            _schema(
                "解析 X.509 证书（只读）：subject/issuer/有效期/SAN/公钥算法。仅证书材料。",
                {
                    "path": {"type": "string", "description": "证书文件路径（可选）"},
                    "pem": {"type": "string", "description": "PEM 文本（可选，与 path 二选一）"},
                },
                [],
            ),
            lambda a: parse_cert(str((a or {}).get("path") or ""), str((a or {}).get("pem") or "")),
        ),
        "pubkey_info": (
            _schema(
                "查看公钥参数（只读，openssl pkey -pubin）：模数/指数/曲线。仅公钥材料，拒绝私钥。",
                {
                    "path": {"type": "string", "description": "公钥文件路径（可选）"},
                    "pem": {"type": "string", "description": "PEM 公钥文本（可选）"},
                },
                [],
            ),
            lambda a: pubkey_info(
                str((a or {}).get("path") or ""), str((a or {}).get("pem") or "")
            ),
        ),
        "rsa_modexp": (
            _schema(
                "RSA 模幂 pow(base, exp, mod)：十进制或 0x 十六进制大整数字符串。纯计算。",
                {
                    "base": {"type": "string", "description": "底数（密文 c 等）"},
                    "exp": {"type": "string", "description": "指数（e/d）"},
                    "mod": {"type": "string", "description": "模数（n/p 等）"},
                },
                ["base", "exp", "mod"],
            ),
            lambda a: rsa_modexp(
                str((a or {}).get("base") or ""),
                str((a or {}).get("exp") or ""),
                str((a or {}).get("mod") or ""),
            ),
        ),
        "hash_compare": (
            _schema(
                "常量时间比对两个哈希/摘要（大小写/0x/冒号分隔符归一化）。",
                {
                    "a": {"type": "string", "description": "摘要 A"},
                    "b": {"type": "string", "description": "摘要 B"},
                },
                ["a", "b"],
            ),
            lambda a: hash_compare(str((a or {}).get("a") or ""), str((a or {}).get("b") or "")),
        ),
    }


def register() -> dict[str, tuple[dict, object]]:
    """admin enable 门控：未设置 SECPT_ENABLE_CRYPTO_HELPERS=1 时不注册任何工具
    （profile 装配时自动缺失，模型不可见；需启用时在环境/启动脚本显式声明）。"""
    if not _enabled():
        return {}
    global _TOOLS
    if not _TOOLS:
        _TOOLS = _build()
    return _TOOLS


__all__ = ["ENABLE_ENV", "hash_compare", "parse_cert", "pubkey_info", "register", "rsa_modexp"]
