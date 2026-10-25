"""共享 fixtures。

陷阱 A5：运行数据（含测试临时目录）统一放工作区内，不依赖系统 tmp。
"""
from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def tmp_mission() -> Path:
    d = ROOT / "missions" / ".test" / uuid.uuid4().hex
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)
