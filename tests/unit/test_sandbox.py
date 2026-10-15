"""Docker 沙箱测试：模式路由 / 命令构造 / 不可用时行为（全部 mock，不依赖真实 docker）。"""

from __future__ import annotations

import subprocess
from typing import ClassVar

import pytest

from secpt.runtime.sandbox import (
    DEFAULT_IMAGE,
    DockerSandbox,
    SandboxUnavailable,
    get_container,
    release_container,
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
    def test_runcfg_default_none(self):
        """阶段一：全局宿主直跑——RunConfig 默认 sandbox=none（不再自动进容器）。"""
        from secpt.runtime.engine import RunConfig

        cfg = RunConfig(job_id="j", objective="o", system_extra="x", workspace="/tmp")
        assert cfg.sandbox == "none"

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


class TestRelativeWorkDir:
    """回归：相对 work_dir 必须被绝对化后再给 docker -v（f242661a 事故）。"""

    def test_relative_path_absolutized(self, tmp_path, monkeypatch):
        import subprocess

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SECPT_SANDBOX", "1")  # 覆盖 conftest 全局关闭
        monkeypatch.setattr("secpt.runtime.sandbox.docker_available", lambda: True)
        monkeypatch.setattr(DockerSandbox, "_image_exists", staticmethod(lambda img: True))
        (tmp_path / "var" / "jobs" / "j1" / "ws").mkdir(parents=True)

        sb = DockerSandbox("j1", work_dir="var/jobs/j1/ws")  # 相对路径！
        run_cmd = {}

        def fake_run(cmd, **kw):
            if cmd[:2] == ["docker", "run"] and "-v" in cmd and "run_cmd" not in run_cmd:
                run_cmd["run_cmd"] = cmd  # 只记 run（readiness 的 exec 会覆盖）
            return subprocess.CompletedProcess(cmd, 0, "container-id", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        sb.start()
        v = run_cmd["run_cmd"][run_cmd["run_cmd"].index("-v") + 1]
        assert v.startswith("/") and v.endswith(":/work"), f"挂载源未绝对化: {v}"


class TestAutoFallbackToHost:
    def test_sandbox_unavailable_auto_runs_host(self, tmp_path, monkeypatch):
        """auto 模式容器起不来 → 回退宿主执行（docker 模式仍硬失败）。"""
        import asyncio
        from unittest.mock import patch

        from secpt.runtime.engine import RunConfig, make_tool_fns
        from secpt.runtime.sandbox import SandboxUnavailable

        cfg = RunConfig(
            job_id="j", objective="o", system_extra="x", workspace=tmp_path, sandbox="auto"
        )
        cfg.work_dir.mkdir(parents=True, exist_ok=True)
        tools = make_tool_fns(cfg)
        monkeypatch.setenv("SECPT_SANDBOX", "1")  # 覆盖 conftest 全局关闭

        with (
            patch("secpt.runtime.sandbox.docker_available", return_value=True),
            patch.object(DockerSandbox, "start", side_effect=SandboxUnavailable("boom")),
        ):
            out = asyncio.new_event_loop().run_until_complete(
                tools["bash"]({"command": "echo fallback-works"})
            )
        assert "fallback-works" in out  # auto 模式容器起不来 → 回退宿主执行

    def test_sandbox_unavailable_docker_mode_hard_fails(self, tmp_path, monkeypatch):
        import asyncio
        from unittest.mock import patch

        from secpt.runtime.engine import RunConfig, make_tool_fns
        from secpt.runtime.sandbox import SandboxUnavailable

        cfg = RunConfig(
            job_id="j2", objective="o", system_extra="x", workspace=tmp_path, sandbox="docker"
        )
        cfg.work_dir.mkdir(parents=True, exist_ok=True)
        tools = make_tool_fns(cfg)
        monkeypatch.setenv("SECPT_SANDBOX", "1")  # 覆盖 conftest 全局关闭
        with (
            patch("secpt.runtime.sandbox.docker_available", return_value=True),
            patch.object(DockerSandbox, "start", side_effect=SandboxUnavailable("boom")),
        ):
            out = asyncio.new_event_loop().run_until_complete(tools["bash"]({"command": "echo x"}))
        assert out.startswith("Error: sandbox 不可用")


class TestWallZeroSemantics:
    """--wall 0 = 显式不限（spec wall_s=-1 → run_engine 收 0）；0 = profile 默认。"""

    def _capture(self, wall_s):
        import asyncio as _a

        from secpt.core.job.models import TaskBudget, TaskSpec
        from secpt.core.orchestrator.dag import run_single_chain

        captured = {}

        class _R:
            flag = None
            steps = 1
            wall_s = 1.0
            status = "step_exceeded"
            messages: ClassVar[list] = []

        import secpt.cli.engine_runner as er

        orig = er.run_engine
        er.run_engine = lambda **kw: (captured.update(kw), _R())[1]
        try:
            spec = TaskSpec(job_id="w1", objective="o", budget=TaskBudget(wall_s=wall_s))
            _a.new_event_loop().run_until_complete(run_single_chain(spec, llm=object()))
        finally:
            er.run_engine = orig
        return captured.get("max_wall_s")

    def test_wall_negative_one_is_unlimited(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        assert self._capture(-1.0) == 0  # 0 = engine 层"不限"

    def test_wall_zero_is_profile_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_VAR", str(tmp_path))
        assert self._capture(0.0) is None  # None → run_engine 用 profile


class TestPersistentContainer:
    def test_reuse_across_calls(self, tmp_path, monkeypatch):
        """同一 job 多次取容器只创建一次（tsec-run-1129 性能修复）。"""
        import subprocess

        monkeypatch.setenv("SECPT_SANDBOX", "1")
        monkeypatch.setattr("secpt.runtime.sandbox.docker_available", lambda: True)
        monkeypatch.setattr(DockerSandbox, "_image_exists", staticmethod(lambda img: True))
        starts = []

        def fake_run(cmd, **kw):
            if cmd[:2] == ["docker", "run"]:
                starts.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "cid", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        sb1 = get_container("pj1", work_dir=str(tmp_path / "w"))
        sb2 = get_container("pj1", work_dir=str(tmp_path / "w"))
        assert sb1 is sb2 and len(starts) == 1  # 只建一次
        release_container("pj1")
        sb3 = get_container("pj1", work_dir=str(tmp_path / "w"))
        assert sb3 is not sb1 and len(starts) == 2  # 释放后重建

    def test_release_idempotent(self, tmp_path, monkeypatch):
        release_container("nonexistent")  # 不炸


def test_engine_releases_container_on_finish(tmp_path, monkeypatch):
    """F2 回归：engine.run 收尾调用 release_container。

    此前 release_container 只有 atexit + 4h TTL 兜底——frontier 每个意图一个
    子 job，逐意图累积常驻容器；现引擎结束即回收（sandbox 未启用时 no-op）。
    """
    import asyncio

    from secpt.core.job.fakellm import FakeLLM
    from secpt.runtime import sandbox as sb
    from secpt.runtime.engine import Engine, RunConfig

    calls: list[str] = []
    monkeypatch.setattr(sb, "release_container", lambda jid: calls.append(jid))
    cfg = RunConfig(job_id="rel1", objective="t", system_extra="", workspace=tmp_path, max_steps=2)
    asyncio.run(Engine(cfg, llm=FakeLLM(n_steps=1, job_id="rel1")).run())
    assert calls == ["rel1"]  # 收尾按 job_id 释放
