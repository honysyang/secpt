"""keyguard 私钥护栏单测。"""

from __future__ import annotations

from secpt.runtime.keyguard import detect_private_key, redact_private_keys

_RSA_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\nrandom\nbody\n"
    "-----END RSA PRIVATE KEY-----\n"
)
_OPENSSH_PEM = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)


def test_detect_and_redact_rsa_block() -> None:
    assert detect_private_key(f"前置 {len(_RSA_PEM)}B\n{_RSA_PEM}\n后置")
    out, n = redact_private_keys(f"A{_RSA_PEM}B")
    assert n == 1
    assert "MIIEpAIBAAKCAQEA" not in out  # 内容不落档
    assert "[REDACTED:private_key:" in out
    assert out.startswith("A") and out.endswith("B")  # 前后文保留


def test_redact_multiple_blocks() -> None:
    out, n = redact_private_keys(f"{_RSA_PEM}mid{_OPENSSH_PEM}")
    assert n == 2
    assert "OPENSSH" not in out and "BEGIN" not in out


def test_clean_text_no_false_positive() -> None:
    clean = (
        "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
        "-----BEGIN PUBLIC KEY-----\nMFwwDQYJ\n-----END PUBLIC KEY-----\n"
        "flag{test_flag} /etc/passwd ssh -i id_rsa\n"
    )
    assert not detect_private_key(clean)
    out, n = redact_private_keys(clean)
    assert n == 0 and out == clean


def test_placeholder_carries_size() -> None:
    out, _ = redact_private_keys(_OPENSSH_PEM)
    import re

    m = re.search(r"\[REDACTED:private_key:(\d+)B\]", out)
    # 尾部换行不属于 PEM 块
    assert m and int(m.group(1)) == len(_OPENSSH_PEM.rstrip("\n"))
