"""全局测试约定：默认关闭 Docker 沙箱（引擎测试不依赖宿主 docker 状态）。

需要测沙箱行为的测试自行 monkeypatch SECPT_SANDBOX / docker_available。
部署类 env（本机 .env / 镜像 --env-file 注入）一律与测试隔离——
它们会改变引擎行为（如 SECPT_MENTOR=1 会让导师调用打乱 FakeLLM 脚本顺序）；
需要测对应行为的测试在用例内自行 setenv。
"""

from __future__ import annotations

import pytest

# 部署类变量清单：只影响运行策略，不属于"引擎默认行为"的一部分
_DEPLOY_ENV_VARS = (
    "SECPT_MENTOR",
    "SECPT_MENTOR_MODEL",
    "SECPT_MENTOR_MAX_CALLS",
    "SECPT_ESCALATE_MODEL",
    "SECPT_VERIFY_MODEL",
    "SECPT_VPN",
    "SECPT_VPN_TYPE",
    "SECPT_VPN_CONFIG",
    "SECPT_TOOL_POOL",
    "SECPT_MAX_TOOL_CALLS",
    "SECPT_MODEL_POOL",
    "ZHIPU_API_KEY",
    "ZHIPU_BASE_URL",
    "JEV_API_KEY",
)


@pytest.fixture(autouse=True)
def _no_sandbox_by_default(monkeypatch):
    monkeypatch.setenv("SECPT_SANDBOX", "0")
    monkeypatch.delenv("SECPT_SANDBOX_MODE", raising=False)


@pytest.fixture(autouse=True)
def _isolate_deploy_env(monkeypatch):
    """隔离部署类 env：本机 .env 的 SECPT_MENTOR=1 等不得改变测试基线行为。"""
    for _k in _DEPLOY_ENV_VARS:
        monkeypatch.delenv(_k, raising=False)


@pytest.fixture(autouse=True)
def _isolate_var_dir(tmp_path_factory, monkeypatch):
    """每用例独立 SECPT_VAR：默认 var/ 与本机真实任务/Web 服务共用同一个
    secpt.sqlite。测试写真实库既污染数据，也会和长跑任务抢 SQLite 写锁
    （实测 database is locked → worker 悬挂）。数据根必须落在临时目录。
    """
    monkeypatch.setenv("SECPT_VAR", str(tmp_path_factory.mktemp("secpt-var")))
