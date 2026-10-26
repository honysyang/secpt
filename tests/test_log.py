"""日志基建测试：get_logger 命名空间强制 / configure 幂等与等级 /
新挂钩点的 emission 断言。

emission 断言（caplog）依赖裸 propagation——所以本文件不调 configure
（conftest 的 _secpt_log_state 会兜底恢复，双保险）；configure 的
行为单独测 handler 数量与等级。

挂钩语义对照（为什么这些点值得测）：它们都是「进不了账本的静默降级」——
账本只记 tool_finished 的 exit/failed 事件，单条降级在数万步日志里
等于没有；这些告警是运维定位配置失约的唯一即时信号。
"""

from __future__ import annotations

import logging

import pytest

from pa import envs
from pa.infra.llm import MockLLM, ModelCallError, complete_retry
from pa.infra.log import configure, get_logger
from pa.tools.mcp import parse_manifest


@pytest.mark.parametrize(
    ("name", "expect"),
    [
        ("secpt.memory", "secpt.memory"),
        ("pa.core.worker", "secpt.pa.core.worker"),
        ("bare", "secpt.bare"),
    ],
)
def test_get_logger_forces_namespace(name: str, expect: str) -> None:
    assert get_logger(name).name == expect


class TestConfigure:
    """configure 行为：幂等（不叠 handler）+ 封闭传播 + 等级生效。"""

    def test_idempotent_no_handler_stack(self) -> None:
        secpt = logging.getLogger("secpt")
        assert not secpt.handlers  # 前置：测试环境裸 logger
        configure("ERROR")
        configure("ERROR")  # 重复调用不得叠加 handler（双打比没有更糟）
        secpt = logging.getLogger("secpt")
        assert len(secpt.handlers) == 1
        assert isinstance(secpt.handlers[0], logging.StreamHandler)

    def test_propagate_closed(self) -> None:
        configure("WARNING")
        assert logging.getLogger("secpt").propagate is False

    def test_level_applied_upper(self) -> None:
        configure("info")
        assert logging.getLogger("secpt").level == logging.INFO

    def test_cli_main_configures(self) -> None:
        """cli.main 入口挂 configure：resume 不存在目录（退出 1），但
        在 argparse / 命令分发之前 configure 已生效。"""
        from pa.cli import main

        assert main(["resume", "<nonexistent-root>"]) == 1
        secpt = logging.getLogger("secpt")
        assert len(secpt.handlers) == 1
        assert secpt.propagate is False


def test_env_log_level_default() -> None:
    assert envs.log_level() == "WARNING"


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        ("DEBUG", "DEBUG"),
        ("info", "INFO"),  # 大小写宽容
        ("ERROR", "ERROR"),
        ("trace", "WARNING"),  # 非法值回保守缺省
        ("", "WARNING"),
    ],
)
def test_env_log_level_values(
    raw: str, expect: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(envs.LOG_LEVEL, raw)
    assert envs.log_level() == expect


def test_complete_retry_warns_each_attempt(caplog: pytest.LogCaptureFixture) -> None:
    """每次失败告警：中间失败原因只在这里可见，进账本的只有最终失败。"""

    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, **_kw: object) -> str:
            self.calls += 1
            raise ModelCallError("boom")

    client = FakeClient()
    with caplog.at_level(logging.WARNING, logger="secpt.llm"):
        with pytest.raises(ModelCallError):
            complete_retry(client, retries=2, system="", user="")
    assert client.calls == 3
    logs = [r.getMessage() for r in caplog.records if r.name == "secpt.llm"]
    assert len(logs) == 3
    assert "第 1/3 次" in logs[0] and "boom" in logs[0]
    assert "第 3/3 次" in logs[2]


def test_parse_manifest_bad_json_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="secpt.mcp"):
        assert parse_manifest("{not json") == []
    assert "不是合法 JSON" in caplog.text


def test_parse_manifest_bad_entries_aggregated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """坏条目聚合一条告警：逐条打会刷屏，静默又丢配置失约现场。"""
    raw = (
        '[{"name":"GOOD","cmd":["python"]},{"name":"ok","cmd":[]},'
        '{"name":"ok2","cmd":["python"],"risk_tier":"nuke"}]'
    )
    with caplog.at_level(logging.WARNING, logger="secpt.mcp"):
        out = parse_manifest(raw)
    assert len(out) == 0  # 全部坏
    message = [r.getMessage() for r in caplog.records if r.name == "secpt.mcp"][0]
    assert "3 条坏条目" in message
    assert "cmd 非法" in message and "risk_tier 非法" in message


def test_parse_manifest_good_entries_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="secpt.mcp"):
        out = parse_manifest('[{"name":"ok","cmd":["python"]}]')
    assert len(out) == 1
    assert not caplog.records


def test_mcp_json_bad_path_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(envs.MCP_JSON, "/nonexistent/manifest.json")
    with caplog.at_level(logging.WARNING, logger="secpt.envs"):
        assert envs.mcp_json() is None
    assert "文件读不到" in caplog.text


def test_mcp_json_inline_ok(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """内联 JSON 正常路径：配置不失约则无声。"""
    monkeypatch.setenv(envs.MCP_JSON, "[]")
    with caplog.at_level(logging.WARNING, logger="secpt.envs"):
        assert envs.mcp_json() == "[]"
    assert not caplog.records


def test_mockllm_repeats_last_script(caplog: pytest.LogCaptureFixture) -> None:
    """complete_retry 只在异常时告警：正常复用最后一条脚本零记录。"""
    llm = MockLLM({"p1": ['{"a":1}']})
    w = dict(system="", user="", schema=None, phase="p1")
    with caplog.at_level(logging.WARNING, logger="secpt.llm"):
        assert complete_retry(llm, retries=1, **w) == '{"a":1}'
        assert complete_retry(llm, retries=1, **w) == '{"a":1}'
    assert not caplog.records
