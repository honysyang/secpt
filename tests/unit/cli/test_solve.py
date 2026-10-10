"""secpt solve 自动化测试：入口抽取 / 附件就位 / 先验匹配 / ground truth 发现。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from secpt.cli.solve import (
    _auto_prior,
    _extract_entries,
    _find_doc,
    _find_ground_truth,
    _prepare_attachments,
)

TEST_MD = """题目：ROIS 公司统一身份认证中心（SAML 单点登录）。
内网地址 : 192.168.1.151
访问环境 :
http 10.30.15.1 56469 服务：http://oj-10-30-15-1-56469.adworld.xctf.org.cn/
http 10.30.15.1 44153 服务：http://oj-10-30-15-1-44153.adworld.xctf.org.cn/
附件：https://example.com/a.zip
"""


class TestFindDoc:
    def test_dir_finds_test_md(self, tmp_path):
        (tmp_path / "test.md").write_text(TEST_MD, encoding="utf-8")
        assert _find_doc(tmp_path).name == "test.md"

    def test_dir_finds_ti_mu(self, tmp_path):
        (tmp_path / "题目.md").write_text(TEST_MD, encoding="utf-8")
        assert _find_doc(tmp_path).name == "题目.md"

    def test_file_directly(self, tmp_path):
        f = tmp_path / "any.md"
        f.write_text(TEST_MD, encoding="utf-8")
        assert _find_doc(f) == f

    def test_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _find_doc(tmp_path)


class TestExtractEntries:
    def test_urls_from_env_lines(self):
        entries = _extract_entries(TEST_MD)
        assert "http://oj-10-30-15-1-56469.adworld.xctf.org.cn/" in entries
        assert "http://oj-10-30-15-1-44153.adworld.xctf.org.cn/" in entries

    def test_plain_url(self):
        assert _extract_entries("目标 https://a.example.com/x 端口 8080")


class TestAttachments:
    def _mk_zip(self, path: Path):
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("src/app.py", "print('hi')")

    def test_zip_extracted(self, tmp_path):
        (tmp_path / "附件").mkdir()
        self._mk_zip(tmp_path / "附件" / "a.zip")
        found = _prepare_attachments(tmp_path)
        assert found
        assert (tmp_path / "attachments" / "a" / "src" / "app.py").is_file()

    def test_path_traversal_blocked(self, tmp_path):
        (tmp_path / "attachments").mkdir()
        with zipfile.ZipFile(tmp_path / "attachments" / "evil.zip", "w") as zf:
            zf.writestr("../../escape.txt", "x")
        _prepare_attachments(tmp_path)
        assert not (tmp_path.parent / "escape.txt").exists()

    def test_query_string_filename(self, tmp_path):
        (tmp_path / "附件").mkdir()
        self._mk_zip(tmp_path / "附件" / "b.zip?attname=real.zip")
        found = _prepare_attachments(tmp_path)
        assert found


class TestAutoPrior:
    def test_saml_hit(self):
        assert _auto_prior(TEST_MD) == "saml"

    def test_sqli_hit(self):
        assert _auto_prior("题目：某系统存在 SQL 注入点") == "sqli"

    def test_miss_returns_none(self):
        assert _auto_prior("一个完全无关的描述") is None


class TestGroundTruth:
    def test_flag_txt(self, tmp_path):
        (tmp_path / "flag.txt").write_text("FLAG{x}", encoding="utf-8")
        assert _find_ground_truth(tmp_path).name == "flag.txt"

    def test_none(self, tmp_path):
        assert _find_ground_truth(tmp_path) is None
