"""H：--json 机器可读输出（jobs / intent list）。"""

from __future__ import annotations

import json

from typer.testing import CliRunner

from secpt.cli.app import app

_runner = CliRunner()


def test_jobs_json(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    result = _runner.invoke(app, ["jobs", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []  # 空库 → 空数组（合法 JSON）


def test_intent_list_json(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    from secpt.core.orchestrator.frontier import Frontier, frontier_db_path

    fr = Frontier(frontier_db_path("j1"))
    fr.add("看看 /admin", source="direct", priority=9)
    result = _runner.invoke(app, ["intent", "list", "--job", "j1", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert rows and rows[0]["title"] == "看看 /admin" and rows[0]["source"] == "direct"


def test_intent_list_json_requires_job(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    result = _runner.invoke(app, ["intent", "list", "--json"])
    assert result.exit_code == 2 and "--job" in result.output
