"""ChallengeLoader 目标去重 + 中文附件目录 + --file/--attach CLI 参数测试。"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from secpt.challenge.loader import Challenge, ChallengeLoader

# ---------- 目标去重 ----------


def _make_challenge(description: str) -> Challenge:
    """构造一个仅用于 extract_targets 的 Challenge。"""
    return Challenge(
        id="test",
        path=Path("/tmp"),
        title="test",
        description=description,
    )


def test_dedup_http_and_bare_host() -> None:
    """http://host:port 与 host:port 视为同一目标。"""
    c = _make_challenge("目标 http://10.30.15.1:54865 和 10.30.15.1:54865")
    targets = c.extract_targets()
    assert len(targets) == 1
    assert targets[0] == "http://10.30.15.1:54865"  # http 形式优先


def test_dedup_bare_host_keeps_bare() -> None:
    """只有 host:port（无 http）时保留原样。"""
    c = _make_challenge("目标 10.30.15.1:54865")
    targets = c.extract_targets()
    assert len(targets) == 1
    assert targets[0] == "10.30.15.1:54865"


def test_dedup_multiple_pairs() -> None:
    """多个目标对各自去重。"""
    c = _make_challenge(
        "http://10.0.0.1:8080 和 10.0.0.1:8080\nhttp://10.0.0.2:9090 和 10.0.0.2:9090"
    )
    targets = c.extract_targets()
    assert len(targets) == 2
    assert "http://10.0.0.1:8080" in targets
    assert "http://10.0.0.2:9090" in targets


def test_dedup_no_duplicates() -> None:
    """无重复时保持原样。"""
    c = _make_challenge("目标 10.0.0.1:80 和 10.0.0.2:443")
    targets = c.extract_targets()
    assert len(targets) == 2


# ---------- 中文附件目录 ----------


def test_chinese_attachment_dir(tmp_path: Path) -> None:
    """中文目录名「附件」应被识别。"""
    challenge_dir = tmp_path / "ch" / "test-chinese"
    challenge_dir.mkdir(parents=True)
    (challenge_dir / "test.md").write_text("# 测试\n目标 10.0.0.1:80\n", encoding="utf-8")

    att_dir = challenge_dir / "附件"
    att_dir.mkdir()
    (att_dir / "data.zip").write_bytes(b"fake-zip-content")
    (att_dir / "notes.txt").write_text("hello", encoding="utf-8")

    loader = ChallengeLoader(base_dir=tmp_path / "ch")
    ch = loader.load("test-chinese")
    assert len(ch.attachments) == 2


def test_english_attachment_dir(tmp_path: Path) -> None:
    """英文目录名 attachments 也能识别。"""
    challenge_dir = tmp_path / "ch" / "test-english"
    challenge_dir.mkdir(parents=True)
    (challenge_dir / "test.md").write_text("# Test\n", encoding="utf-8")

    att_dir = challenge_dir / "attachments"
    att_dir.mkdir()
    (att_dir / "f.bin").write_bytes(b"\x00")

    loader = ChallengeLoader(base_dir=tmp_path / "ch")
    ch = loader.load("test-english")
    assert len(ch.attachments) == 1


def test_no_attachment_dir(tmp_path: Path) -> None:
    """无附件目录时 attachments 为空列表，不报错。"""
    challenge_dir = tmp_path / "ch" / "test-noatt"
    challenge_dir.mkdir(parents=True)
    (challenge_dir / "test.md").write_text("# Test\n", encoding="utf-8")

    loader = ChallengeLoader(base_dir=tmp_path / "ch")
    ch = loader.load("test-noatt")
    assert ch.attachments == []


# ---------- CLI --file / --attach ----------

runner = CliRunner()


def test_cli_run_file_and_task_mutex(tmp_path: Path) -> None:
    """run shim：--file 与 --task 同时给出时使用题面文件并提示（转发 solve）。"""
    from unittest.mock import patch

    from secpt.cli.app import app

    f = tmp_path / "task.txt"
    f.write_text("do something", encoding="utf-8")

    with patch("secpt.cli.solve.solve", return_value=0) as mock_solve:
        result = runner.invoke(
            app,
            [
                "run",
                "10.0.0.1",
                "--mode",
                "tree",
                "--file",
                str(f),
                "--task",
                "other task",
            ],
        )
    assert result.exit_code == 0
    assert "已忽略" in result.output
    mock_solve.assert_called_once()
    assert mock_solve.call_args.args[0] == str(f)


def test_cli_run_tree_deprecated_redirects_to_solve(tmp_path: Path) -> None:
    """secpt run --mode tree 已废弃：提示并自动转发 solve（--file 作为题面）。"""
    from unittest.mock import patch

    from secpt.cli.app import app

    f = tmp_path / "task.txt"
    f.write_text("scan the target", encoding="utf-8")

    with patch("secpt.cli.solve.solve", return_value=0) as mock_solve:
        result = runner.invoke(
            app,
            [
                "run",
                "10.0.0.1",
                "--mode",
                "tree",
                "--file",
                str(f),
            ],
        )
    assert result.exit_code == 0
    assert "已废弃" in result.output
    mock_solve.assert_called_once()
    assert mock_solve.call_args.args[0] == str(f)
    assert "mode" not in mock_solve.call_args.kwargs  # S6：solve 已无 mode 参数


def test_cli_engage_file_and_objective_mutex(tmp_path: Path) -> None:
    """secpt engage 已废弃（S5 删除 evaluator/node），应提示并退出。"""
    from secpt.cli.app import app

    f = tmp_path / "obj.txt"
    f.write_text("engage objective", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "engage",
            "--target",
            "10.0.0.1",
            "--file",
            str(f),
            "--objective",
            "other",
        ],
    )
    assert result.exit_code == 1
    assert "已废弃" in result.output


def test_cli_engage_file_ok(tmp_path: Path) -> None:
    """secpt engage 已废弃（S5 起由 orchestrator 取代）。"""
    from secpt.cli.app import app

    f = tmp_path / "obj.txt"
    f.write_text("penetrate the SAML SSO", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "engage",
            "--target",
            "10.0.0.1",
            "--file",
            str(f),
            "--fake",
        ],
    )
    assert result.exit_code == 1
    assert "已废弃" in result.output


def test_cli_engage_default_objective(tmp_path: Path) -> None:
    """不传 --file / --objective 时 engage 同样已废弃。"""
    from secpt.cli.app import app

    result = runner.invoke(
        app,
        [
            "engage",
            "--target",
            "10.0.0.1",
            "--fake",
        ],
    )
    assert result.exit_code == 1
    assert "已废弃" in result.output
