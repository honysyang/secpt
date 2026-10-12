"""全局测试约定：默认关闭 Docker 沙箱（引擎测试不依赖宿主 docker 状态）。

需要测沙箱行为的测试自行 monkeypatch SECPT_SANDBOX / docker_available。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_sandbox_by_default(monkeypatch):
    monkeypatch.setenv("SECPT_SANDBOX", "0")
    monkeypatch.delenv("SECPT_SANDBOX_MODE", raising=False)
