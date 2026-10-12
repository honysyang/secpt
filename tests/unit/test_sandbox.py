"""Docker 沙箱测试：模式路由 / 命令构造 / 不可用时行为（全部 mock，不依赖真实 docker）。"""

from __future__ import annotations

import subprocess

import pytest

from secpt.runtime.sandbox import (
    DEFAULT_IMAGE,
    DockerSandbox,
    SandboxUnavailable,
    sandbox_scope,
)


class TestScopeRouting:
    def test_none_mode(self):
        with sandbox_scope("j1", work_dir="/tmp", mode="none") as sb:
            assert sb is None

    def test_env_disable(self, monkeypatch):
        monkeypatch.setenv("SECPT_SANDBOX", "0")
        monkeypatch.setattr("secpt.runtime.sandbox.docker_available", lambda: True)
        with sandbox_scope("j1", work_dir="/tmp", mode="auto") as sb:
            assert sb is None

    def test_env_force_mode(self, monkeypatch):
        monkeypatch.setenv("SECPT_SANDBOX_MODE", "none")
        with sandbox_scope("j1", work_dir="/tmp", mode="docker") as sb:
            assert sb is None  # env 强制 none 覆盖 docker

    def test_unavailable_auto_falls_back(self, monkeypatch):
        monkeypatch.delenv("SECPT_SANDBOX", raising=False)
        monkeypatch.setattr("secpt.runtime.sandbox.docker_available", lambda: False)
        with sandbox_scope("j1", work_dir="/tmp", mode="auto") as sb:
            assert sb is None

    def test_unavailable_forced_raises(self, monkeypatch):
        monkeypatch.setenv("SECPT_SANDBOX", "1")  # 覆盖 conftest 的全局关闭
        monkeypatch.setattr("secpt.runtime.sandbox.docker_available", lambda: False)
        with pytest.raises(SandboxUnavailable), sandbox_scope("j1", work_dir="/tmp", mode="docker"):
            pass


class TestExecConstruction:
    def _mk(self, tmp_path, monkeypatch, image_exists=True):
        monkeypatch.setattr(DockerSandbox, "_image_exists", staticmethod(lambda img: image_exists))
        return DockerSandbox("jobx", work_dir=str(tmp_path))

    def test_image_fallback(self, tmp_path, monkeypatch):
        sb = self._mk(tmp_path, monkeypatch, image_exists=False)
        assert sb.image != DEFAULT_IMAGE  # 未构建 → 回退基础镜像

    def test_exec_argv(self, tmp_path, monkeypatch):
        sb = self._mk(tmp_path, monkeypatch)
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)

            class R:
                returncode = 0
                stdout = "hello"
                stderr = ""

            return R()

        monkeypatch.setattr(subprocess, "run", fake_run)
        rc, out, err = sb.exec(
            ["curl", "-s", "http://t/"], timeout_s=30, env={"HTTP_PROXY": "http://p:1"}
        )
        assert (rc, out, err) == (0, "hello", "")
        cmd = calls[0]
        assert cmd[:2] == ["docker", "exec"]
        assert "-e" in cmd and "HTTP_PROXY=http://p:1" in cmd
        assert cmd[-3:] == ["curl", "-s", "http://t/"]
        assert sb.name in cmd

    def test_exec_timeout_rc(self, tmp_path, monkeypatch):
        sb = self._mk(tmp_path, monkeypatch)

        def fake_run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 35)

        monkeypatch.setattr(subprocess, "run", fake_run)
        rc, _out, err = sb.exec(["sleep", "99"], timeout_s=30)
        assert rc == 124 and "timeout" in err


class TestEngineIntegration:
    def test_runcfg_default_auto(self):
        from secpt.runtime.engine import RunConfig

        cfg = RunConfig(job_id="j", objective="o", system_extra="x", workspace="/tmp")
        assert cfg.sandbox == "auto"

    def test_bash_sandbox_unavailable_falls_back(self, tmp_path, monkeypatch):
        """auto + docker 不可用 → 静默回退宿主执行（不炸任务）。"""
        import asyncio

        from secpt.runtime.engine import RunConfig, make_tool_fns

        monkeypatch.setattr("secpt.runtime.sandbox.docker_available", lambda: False)
        cfg = RunConfig(job_id="j", objective="o", system_extra="x", workspace=tmp_path)
        cfg.work_dir.mkdir(parents=True, exist_ok=True)
        tools = make_tool_fns(cfg)

        async def go():
            return await tools["bash"]({"command": "echo hello_host"})

        out = asyncio.new_event_loop().run_until_complete(go())
        assert "hello_host" in out
