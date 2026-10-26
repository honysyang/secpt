"""STATUS.md 引用完整性：每个 ✅ 行点名的 `test_*` 必须真实存在。"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_TEST_RE = re.compile(r"`(test_[A-Za-z0-9_]+)`")


def _all_test_names() -> set[str]:
    names: set[str] = set()
    for p in (ROOT / "tests").glob("test_*.py"):
        names.update(re.findall(r"^def (test_[A-Za-z0-9_]+)\(", p.read_text(
            encoding="utf-8"), flags=re.MULTILINE))
    return names


def test_status_referenced_tests_exist():
    status = (ROOT / "STATUS.md").read_text(encoding="utf-8")
    refs = set(_TEST_RE.findall(status))
    assert refs, "STATUS.md 至少要点名一个测试"
    missing = refs - _all_test_names()
    assert not missing, f"STATUS.md 引用了不存在的测试: {sorted(missing)}"


def test_status_every_green_row_names_a_test():
    """每个 ✅ 行的「验收测试」列必须有 `test_*` 函数名——空 ✅ 不许过。"""
    status = (ROOT / "STATUS.md").read_text(encoding="utf-8")
    for line in status.splitlines():
        if line.startswith("|") and "✅" in line:
            assert _TEST_RE.search(line), f"✅ 行缺测试名: {line}"
