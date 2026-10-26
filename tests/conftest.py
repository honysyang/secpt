"""共享 fixtures。

陷阱 A5：运行数据（含测试临时目录）统一放工作区内，不依赖系统 tmp。

陷阱 L1：logging 是进程级全局状态——cli.main 会 configure ``secpt``
根 logger（挂 handler + propagate=False），不回滚会让后续依赖
propagation 的 caplog 断言（test_experience / test_report 等）收不到
记录。autouse fixture 在每个测试结束后恢复原状态，保证 configure
只在「章内」生效、不污染别的章节。
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _secpt_log_state():
    secpt = logging.getLogger("secpt")
    saved = (secpt.level, secpt.propagate, list(secpt.handlers))
    yield
    secpt.handlers[:] = saved[2]
    secpt.propagate = saved[1]
    secpt.setLevel(saved[0])


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def tmp_mission() -> Path:
    d = ROOT / "missions" / ".test" / uuid.uuid4().hex
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)
