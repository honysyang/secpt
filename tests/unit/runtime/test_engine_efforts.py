"""C/E：read_file 分页 + 并行上限可配置。"""

from __future__ import annotations

from secpt.runtime.engine import READ_HARD_CAP, RunConfig, _parallel_max, make_tool_fns


def _tools(tmp_path):
    cfg = RunConfig(job_id="eff", objective="t", system_extra="", workspace=tmp_path)
    return make_tool_fns(cfg)


class TestReadFilePaging:
    def test_small_file_unpaged(self, tmp_path):
        (tmp_path / "ws").mkdir(exist_ok=True)
        (tmp_path / "ws" / "a.txt").write_text("hello world", encoding="utf-8")
        out = _tools(tmp_path)["read_file"]({"path": "a.txt"})
        assert out == "hello world"  # 小文件无续读提示

    def test_offset_limit_slice_and_note(self, tmp_path):
        (tmp_path / "ws").mkdir(exist_ok=True)
        content = "0123456789" * 10  # 100 字符
        (tmp_path / "ws" / "big.txt").write_text(content, encoding="utf-8")
        out = _tools(tmp_path)["read_file"]({"path": "big.txt", "offset": 10, "limit": 5})
        assert out.startswith("01234")
        assert "已读 10-15/100" in out and "offset=15" in out  # 续读提示含下一页 offset

    def test_offset_beyond_end(self, tmp_path):
        (tmp_path / "ws").mkdir(exist_ok=True)
        (tmp_path / "ws" / "a.txt").write_text("abc", encoding="utf-8")
        out = _tools(tmp_path)["read_file"]({"path": "a.txt", "offset": 99})
        assert out.startswith("Error:") and "超出文件长度" in out

    def test_bad_offset_type(self, tmp_path):
        (tmp_path / "ws").mkdir(exist_ok=True)
        (tmp_path / "ws" / "a.txt").write_text("abc", encoding="utf-8")
        out = _tools(tmp_path)["read_file"]({"path": "a.txt", "limit": "abc"})
        assert out.startswith("Error:") and "整数" in out

    def test_empty_file(self, tmp_path):
        (tmp_path / "ws").mkdir(exist_ok=True)
        (tmp_path / "ws" / "e.txt").write_text("", encoding="utf-8")
        assert _tools(tmp_path)["read_file"]({"path": "e.txt"}) == "(empty file)"

    def test_huge_file_rejected_sparse(self, tmp_path):
        (tmp_path / "ws").mkdir(exist_ok=True)
        p = tmp_path / "ws" / "huge.bin"
        with open(p, "wb") as f:
            f.truncate(READ_HARD_CAP + 1)  # 稀疏文件：不实际占用磁盘
        out = _tools(tmp_path)["read_file"]({"path": "huge.bin"})
        assert out.startswith("Error:") and "head/tail/sed" in out


class TestParallelMax:
    def test_default_and_clamp(self, monkeypatch):
        monkeypatch.delenv("SECPT_PARALLEL_MAX", raising=False)
        assert _parallel_max() == 4
        monkeypatch.setenv("SECPT_PARALLEL_MAX", "8")
        assert _parallel_max() == 8
        monkeypatch.setenv("SECPT_PARALLEL_MAX", "99")
        assert _parallel_max() == 16  # 上限
        monkeypatch.setenv("SECPT_PARALLEL_MAX", "0")
        assert _parallel_max() == 1  # 下限
        monkeypatch.setenv("SECPT_PARALLEL_MAX", "abc")
        assert _parallel_max() == 4  # 非法值回退默认
