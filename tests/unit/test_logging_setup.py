"""M5-34 日志装配：行缓冲 / 级别过滤 / SECPT_LOG_FILE 审计双写。"""

from __future__ import annotations

import json

import pytest
import structlog

from secpt.ob.logging_setup import configure_logging


@pytest.fixture()
def fresh_structlog():
    yield
    structlog.reset_defaults()


def test_log_file_receives_json_events(tmp_path, monkeypatch, fresh_structlog) -> None:
    """SECPT_LOG_FILE：JSON 事件同落审计文件——审计不再依赖用户记得 tee。"""
    logfile = tmp_path / "audit.jsonl"
    monkeypatch.setenv("SECPT_LOG_FILE", str(logfile))
    configure_logging()
    log = structlog.get_logger("m534")
    log.info("run_step_started", task_id="t1", step_no=1)

    lines = [line for line in logfile.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines, "审计文件应有内容"
    event = json.loads(lines[-1])
    assert event["event"] == "run_step_started"
    assert event["task_id"] == "t1"


def test_log_level_filters_info_when_warning(tmp_path, monkeypatch, fresh_structlog) -> None:
    """SECPT_LOG_LEVEL=WARNING：info 事件不再输出（stderr 与文件双沉默）。"""
    logfile = tmp_path / "audit.jsonl"
    monkeypatch.setenv("SECPT_LOG_FILE", str(logfile))
    monkeypatch.setenv("SECPT_LOG_LEVEL", "WARNING")
    configure_logging()
    structlog.get_logger("m534").info("should_be_dropped")
    assert logfile.read_text(encoding="utf-8") == ""


def test_streams_are_line_buffered_after_configure(fresh_structlog) -> None:
    """行缓冲：tee 合流不再粘行（M5-34 修的正是 JSON 粘在气泡行尾的废数据）。"""
    configure_logging()
    import sys

    assert sys.stdout.line_buffering is True
    assert sys.stderr.line_buffering is True


def test_bad_log_level_falls_back_to_info(monkeypatch, fresh_structlog) -> None:
    """非法级别不拒跑：日志配置是基础设施，静默回落 INFO。"""
    monkeypatch.setenv("SECPT_LOG_LEVEL", "LOUD")
    configure_logging()  # 不抛
    structlog.get_logger("m534").info("still works")
