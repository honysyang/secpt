"""文件类工具单测：相对路径落到工作目录，路径逃逸被拒。

验收（任务 D）：
  work_dir=<workspace>/ws（对应验收里的 /tmp/t/ws）
  - write_file("a.py","x") 必须成功且落在工作目录下
  - write_file("/etc/passwd","x") 必须被拒
  - write_file("../escape.py","x") 必须被拒

迁移性补充（任务 E）：
  - read_file / list_dir 相对路径同样落到工作目录（与 bash cwd 一致）
"""

from __future__ import annotations

from pathlib import Path

from secpt.runtime.engine import RunConfig, make_tool_fns


def _make_fns(ws: Path):
    cfg = RunConfig(
        job_id="test-write-file",
        objective="test",
        system_extra="",
        workspace=ws,
    )
    cfg.work_dir.mkdir(parents=True, exist_ok=True)  # 引擎正常流程中目录已存在
    return make_tool_fns(cfg), cfg.work_dir


def test_write_file_relative_path_lands_in_workdir(tmp_path):
    fns, work_dir = _make_fns(tmp_path)
    out = fns["write_file"]({"path": "a.py", "content": "x"})
    assert out.startswith("OK: wrote")
    # 必须落在 <workspace>/ws/a.py
    assert (work_dir / "a.py").read_text() == "x"


def test_write_file_absolute_escape_rejected(tmp_path):
    fns, work_dir = _make_fns(tmp_path)
    out = fns["write_file"]({"path": "/etc/passwd", "content": "x"})
    assert out.startswith("Error:")
    # 工作目录里不应出现任何文件
    assert list(work_dir.iterdir()) == []


def test_write_file_dotdot_escape_rejected(tmp_path):
    fns, work_dir = _make_fns(tmp_path)
    out = fns["write_file"]({"path": "../escape.py", "content": "x"})
    assert out.startswith("Error:")
    # 不能逃到工作目录之外
    assert not (work_dir.parent / "escape.py").exists()
    assert list(work_dir.iterdir()) == []


def test_read_listdir_relative_paths_resolve_to_workdir(tmp_path):
    fns, work_dir = _make_fns(tmp_path)
    # 写一个嵌套文件，再用相对路径读/列
    (work_dir / "sub").mkdir()
    (work_dir / "sub" / "x.txt").write_text("hi", encoding="utf-8")
    out = fns["read_file"]({"path": "sub/x.txt"})
    assert "hi" in out
    listing = fns["list_dir"]({"path": "sub"})
    assert "x.txt" in listing
    # 绝对路径读也兼容
    out_abs = fns["read_file"]({"path": str(work_dir / "sub" / "x.txt")})
    assert "hi" in out_abs
