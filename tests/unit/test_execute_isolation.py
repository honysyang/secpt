"""M3-5 执行隔离（先测试）：Docker 隔离与 runner seam 的钉子。

蓝本锚点：goals 文"后续补执行隔离"；拍板②（§71 前口径）：
- isolation="shell"|"docker" 入 profile.toml [execute] 段 + fail-closed——docker
  模式把 argv 包成 [docker, run, --rm, <image>, <argv>] 交给 DockerRunner 执行；
- docker bin/镜像缺失 → E_TOOL_FAIL 回执、绝不静默退回本机执行（宿主方案不
  可信 = 拒绝比兜底诚实），宿主 shim 未被执行是本档的"没悄悄落本机"铁证；
- 缺 [execute] 段/缺文件 = shell 档（isolation 不开），M1/M2 行为逐字节守恒。

runner seam 契约：NmapTool 增设注入点 runner（None = LocalRunner 现行为），
build_docker_argv 是纯函数（无 subprocess 无副作用）。
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from secpt.errors import SecptError
from secpt.tools.adapters import NmapTool
from secpt.tools.executors import (
    DockerRunner,
    IsolationConfig,
    LocalRunner,
    build_docker_argv,
    load_isolation_config,
)


def _shim(tmp_path: Path, name: str, body: str) -> str:
    """写一个壳脚本替身并提可执行位；$@ 语义被各用例复用。"""
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


# ---------- build_docker_argv（纯函数） --------------------------------------------


def test_build_docker_argv_wraps_full_command() -> None:
    """argv 序 = [docker, run, --rm, image, *argv]：原始 argv 原样垫底不移心。"""
    argv = build_docker_argv(["/usr/bin/nmap", "-Pn", "10.0.0.1"], image="img-x")
    assert argv == ["docker", "run", "--rm", "img-x", "/usr/bin/nmap", "-Pn", "10.0.0.1"]


def test_build_docker_argv_custom_docker_bin() -> None:
    """docker_bin 可指派（mama 场景 /usr/local/bin/docker），入呼叫序不吞。"""
    argv = build_docker_argv(
        ["/bin/nmap", "t"], image="secpt-sandbox", docker_bin="/usr/local/bin/docker"
    )
    assert argv[0] == "/usr/local/bin/docker"
    assert argv[1:4] == ["run", "--rm", "secpt-sandbox"]


# ---------- LocalRunner（现行为守恒） ----------------------------------------------


def test_local_runner_runs_host_shim(tmp_path: Path) -> None:
    """LocalRunner 子进程出壳：returncode/stdout 如实回填 CompletedProcess。"""
    echo_shim = _shim(tmp_path, "echoer", 'echo "$@"')
    proc = LocalRunner().run([echo_shim, "hello", "world"], timeout_s=5.0)
    assert isinstance(proc, subprocess.CompletedProcess)
    assert proc.returncode == 0
    assert proc.stdout == "hello world\n"


def test_nmap_none_runner_equals_local_runner(tmp_path: Path) -> None:
    """runner 未注入 vs 显式 LocalRunner：回执逐字段守恒（seam 不偏移现行为）。"""
    nmap_shim = _shim(tmp_path, "nmap-base", "echo '22/tcp open  ssh'")
    plain = NmapTool(binary=nmap_shim, raw_dir=tmp_path / "r1").run({"target": "t1"})
    injected = NmapTool(binary=nmap_shim, raw_dir=tmp_path / "r2", runner=LocalRunner()).run(
        {"target": "t1"}
    )
    assert plain.exit_code == injected.exit_code == 0
    assert plain.summary == injected.summary == "22/tcp open  ssh"
    for ref in (plain.raw_ref, injected.raw_ref):
        assert ref
        assert Path(ref).is_file()


# ---------- DockerRunner（argv 序 + fail-closed） ----------------------------------


def test_docker_runner_forwards_argv_in_order(tmp_path: Path) -> None:
    """docker shim 收到的参数序 = run --rm <image> <原始 argv>（一字不差）。"""
    docker_shim = _shim(tmp_path, "docker", 'echo "$@"')
    runner = DockerRunner(image="secpt-sandbox", docker_bin=docker_shim)
    proc = runner.run(["/bin/nmap-fake", "-Pn", "10.0.0.8"], timeout_s=5.0)
    assert proc.returncode == 0
    assert proc.stdout.split() == [
        "run",
        "--rm",
        "secpt-sandbox",
        "/bin/nmap-fake",
        "-Pn",
        "10.0.0.8",
    ]


def test_nmap_docker_fail_closed_no_host_fallback(tmp_path: Path) -> None:
    """fail-closed：docker bin 缺失 → E_TOOL_FAIL 回执，宿主 shim 从未被执行。

    marker 文件是"宿主方案没被静默执行"的铁证：若实现偷偷退回本机跑，
    nmap shim 的 touch 会落 marker——本测试对空 marker 文件零容忍。
    """
    marker = tmp_path / "host_shim_marker"
    nmap_shim = _shim(tmp_path, "nmap-host", f'touch "{marker}"\necho 22/tcp open  ssh')
    ghost_docker = str(tmp_path / "docker 不存在的路径")
    registry_args: Mapping[str, str] = {"target": "10.0.0.9"}
    receipt = NmapTool(
        binary=nmap_shim,
        raw_dir=tmp_path / "raw",
        timeout_s=5.0,
        runner=DockerRunner(image="secpt-sandbox", docker_bin=ghost_docker),
    ).run(registry_args)
    assert receipt.exit_code is None
    assert receipt.summary.startswith("E_TOOL_FAIL")
    assert "docker" in receipt.summary
    assert not marker.exists()


def test_nmap_docker_image_missing_is_honest_rc125(tmp_path: Path) -> None:
    """镜像缺失（docker rc=125）：回执如实透传 exit_code=125 + stderr 原文。"""
    docker_shim = _shim(tmp_path, "docker-125", 'echo "cannot find image" >&2\nexit 125')
    receipt = NmapTool(
        binary="/bin/nmap-fake",
        raw_dir=tmp_path / "raw",
        timeout_s=5.0,
        runner=DockerRunner(image="no-such-image", docker_bin=docker_shim),
    ).run({"target": "10.0.0.9"})
    assert receipt.exit_code == 125
    assert "cannot find image" in receipt.summary
    assert receipt.raw_ref
    assert Path(receipt.raw_ref).is_file()


# ---------- load_isolation_config（[execute] 段三态） ------------------------------


def test_isolation_defaults_when_missing(tmp_path: Path) -> None:
    """缺文件/缺 [execute] 段 = shell 档默认镜像（M1/M2 行为逐字节守恒）。"""
    assert load_isolation_config(None) == IsolationConfig()
    snake = load_isolation_config(str(tmp_path / "ghost.toml"))
    assert snake == IsolationConfig()
    bare = tmp_path / "profile.toml"
    bare.write_text("[judge]\nenabled = true\n", encoding="utf-8")
    assert load_isolation_config(str(bare)) == IsolationConfig()
    default = IsolationConfig()
    assert default.isolation == "shell"
    assert default.image == "secpt-sandbox"
    assert default.docker_bin == "docker"


def test_isolation_section_loads(tmp_path: Path) -> None:
    """[execute] 段如实解析：isolation/image/docker_bin 三字段各自入座。"""
    path = tmp_path / "profile.toml"
    path.write_text(
        '[execute]\nisolation = "docker"\nimage = "secpt-sandbox-pwn"'
        '\ndocker_bin = "/usr/local/bin/docker"\n',
        encoding="utf-8",
    )
    cfg = load_isolation_config(str(path))
    assert cfg.isolation == "docker"
    assert cfg.image == "secpt-sandbox-pwn"
    assert cfg.docker_bin == "/usr/local/bin/docker"


def test_isolation_bad_value_rejects(tmp_path: Path) -> None:
    """isolation 拼错 = 配置笔误，同 judge/guard 手式 E_PROFILE 拒跑不静默。"""
    path = tmp_path / "profile.toml"
    path.write_text('[execute]\nisolation = "kubernetes"\n', encoding="utf-8")
    with pytest.raises(SecptError) as excinfo:
        load_isolation_config(str(path))
    assert excinfo.value.code == "E_PROFILE"
