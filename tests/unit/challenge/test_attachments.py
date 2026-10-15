"""附件文件名净化测试：?attname= 查询串剥离 + Windows 禁止字形替换。

Windows 文件系统禁止 ? * < > | " 字形，落盘即 OSError——净化后的文件名
必须不含任一禁止字形（本组断言在 Linux 上即可覆盖 Windows 命名约束）。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from secpt.challenge.attachments import (
    AttachmentHandler,
    sanitize_filename,
)

_WIN_FORBIDDEN = '?*<>|"'


def _mk_zip(path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


class TestSanitizeFilename:
    def test_query_string_stripped_keeps_ext(self):
        assert sanitize_filename("b.zip?attname=real.zip") == "b.zip"
        assert sanitize_filename("pkg.tar.gz?attname=x") == "pkg.tar.gz"

    def test_windows_forbidden_chars_replaced(self):
        # Windows 禁止字形（含兜底的 < > : " | *）不得出现在净化结果中
        dirty = 'a<b>c:d"e|f*g?h.rar'
        clean = sanitize_filename(dirty)
        assert not any(ch in clean for ch in _WIN_FORBIDDEN)

    def test_trailing_dot_space_stripped(self):
        # Windows 不允许文件名以点/空格结尾
        assert sanitize_filename("file. ") == "file"
        assert sanitize_filename("name ") == "name"

    def test_control_chars_replaced(self):
        assert sanitize_filename("a\x01b.txt") == "a_b.txt"

    def test_empty_becomes_placeholder(self):
        assert sanitize_filename("?attname=x") == "attachment"
        assert sanitize_filename("") == "attachment"

    def test_normal_name_unchanged(self):
        assert sanitize_filename("source-code_v2.zip") == "source-code_v2.zip"


class TestSafeRelParts:
    def test_keeps_directory_structure(self):
        from secpt.challenge.attachments import _safe_rel_parts

        assert _safe_rel_parts("dir/sub/file.txt") == ["dir", "sub", "file.txt"]

    def test_drops_traversal_and_absolute(self):
        from secpt.challenge.attachments import _safe_rel_parts

        assert _safe_rel_parts("../../etc/passwd") == ["etc", "passwd"]
        assert _safe_rel_parts("/abs/path.txt") == ["abs", "path.txt"]
        assert _safe_rel_parts("a\\b.txt") == ["a", "b.txt"]

    def test_entry_query_string_stripped(self):
        from secpt.challenge.attachments import _safe_rel_parts

        assert _safe_rel_parts("doc?dl=1.txt") == ["doc"]


class TestExtractSanitized:
    def test_zip_entry_with_query_string(self, tmp_path):
        """zip 内 entry 名含查询串：'?' 起整段剥离，落盘名净化（Windows 不再 OSError）。"""
        zpath = tmp_path / "附件.zip"
        _mk_zip(
            zpath,
            {"b.zip?attname=real.zip": b"zz", "src/main.py?x=1": b"print(1)"},
        )
        out = AttachmentHandler(tmp_path / "out").extract(zpath)
        names = [p.name for p in out]
        assert "b.zip" in names  # 扩展名在 ? 前 → 保留（b.zip?attname=real.zip → b.zip）
        assert "main.py" in names  # 目录结构保留，entry 查询串剥离
        for p in out:
            assert not any(ch in p.name for ch in _WIN_FORBIDDEN)

    def test_zip_traversal_entry_no_escape(self, tmp_path):
        """穿越 entry（../ 段被剥离）不逃逸出解压目录。"""
        zpath = tmp_path / "evil.zip"
        _mk_zip(zpath, {"../escape.txt": b"x", "ok.txt": b"ok"})
        out = AttachmentHandler(tmp_path / "out").extract(zpath)
        assert {p.name for p in out} <= {"escape.txt", "ok.txt"}
        assert not (tmp_path / "escape.txt").exists()  # 未逃逸到解压目录之外

    def test_tar_entry_with_query_string(self, tmp_path):
        import tarfile

        tpath = tmp_path / "pkg.tar"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            data = b"data"
            ti = tarfile.TarInfo("doc?dl=1.txt")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        tpath.write_bytes(buf.getvalue())
        out = AttachmentHandler(tmp_path / "out").extract(tpath)
        # 'doc?dl=1.txt'：扩展名在查询串内不可恢复 → 剥后主干 "doc"
        assert [p.name for p in out] == ["doc"]
        assert out[0].read_bytes() == b"data"

    def test_outer_zip_filename_with_query_string(self, tmp_path):
        """附件包本体名带查询串（Linux 已落盘场景）：解压目录名净化。"""
        zpath = tmp_path / "b.zip?attname=real.zip"
        _mk_zip(zpath, {"f.txt": b"ok"})
        out = AttachmentHandler(tmp_path / "out").extract(zpath)
        assert out and out[0].name == "f.txt"
        # 解压目录名不含禁止字形
        assert not any(ch in str(out[0].parent.name) for ch in _WIN_FORBIDDEN)
