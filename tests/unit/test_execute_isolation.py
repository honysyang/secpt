"""M3-5 执行隔离 + M4-5 容器加固（先测试）：Docker 隔离与 runner seam 的钉子。

蓝本锚点：goals 文"后续补执行隔离"；拍板②（§71 前口径）：
- isolation="shell"|"docker" 入 profile.toml [execute] 段 + fail-closed——docker
  模式把 argv 包成 [docker, run, --rm, *flags, <image>, <argv>] 交给
  DockerRunner 执行；
- docker bin/镜像缺失 → E_TOOL_FAIL 回执、绝不静默退回本机执行（宿主方案不
  可信 = 拒绝比兜底诚实），宿主 shim 未被执行是本档的"没悄悄落本机"铁证；
- 缺 [execute] 段/缺文件 = shell 档（isolation 不开），M1/M2 行为逐字节守恒。

M4-5 容器加固（§71 遗留④）：docker run 默认垫四连加固 flags（--network=none
/ --memory=512m / --read-only / --cap-drop=ALL），flags 位置固定在 --rm 与
image 之间（顺序稳定可测）；[execute].docker_flags / step_tool_limit 可配；
真镜像无网集成测试证"容器内 curl 失败且宿主服务零请求"（对照组 bridge 可达，
证明失败确实是 --network=none 掐的，不是环境残废）。

runner seam 契约：NmapTool 增设注入点 runner（None = LocalRunner 现行为），
build_docker_argv 是纯函数（无 subprocess 无副作用）。
"""

from __future__ import annotations

import http.server
import shutil
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path

import pytest

from secpt.errors import SecptError
from secpt.tools.adapters import NmapTool
from secpt.tools.executors import (
    DEFAULT_DOCKER_FLAGS,
    DockerRunner,
    IsolationConfig,
    LocalRunner,
    build_docker_argv,
    load_isolation_config,
)

_DEFAULT_FLAGS = [
    "--network=none",
    "--memory=512m",
    "--read-only",
    "--cap-drop=ALL",
]


def _shim(tmp_path: Path, name: str, body: str) -> str:
    """写一个壳脚本替身并提可执行位；$@ 语义被各用例复用。"""
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


# ---------- build_docker_argv（纯函数） --------------------------------------------


def test_build_docker_argv_wraps_full_command() -> None:
    """M4-5 加固默认：argv 序 = [docker, run, --rm, *四连 flags, image, *argv]。

    flags 位置固定在 --rm 与 image 之间——容器内看到的就是宿主本来要跑的
    那串命令一字不差，加固 flags 也绝不挤进容器 argv（M3-5 纪律延续）。
    """
    argv = build_docker_argv(["/usr/bin/nmap", "-Pn", "10.0.0.1"], image="img-x")
    assert argv == [
        "docker",
        "run",
        "--rm",
        *_DEFAULT_FLAGS,
        "img-x",
        "/usr/bin/nmap",
        "-Pn",
        "10.0.0.1",
    ]
    assert list(DEFAULT_DOCKER_FLAGS) == _DEFAULT_FLAGS  # 默认序与任务书四连一致


def test_build_docker_argv_flags_override_preserves_order() -> None:
    """[execute].docker_flags 可配：flags 序 = 配置序（顺序稳定可测），容器 argv 不移心。"""
    argv = build_docker_argv(
        ["/bin/nmap", "t"],
        image="img-x",
        flags=["--memory=1g", "--network=bridge"],
    )
    assert argv == [
        "docker",
        "run",
        "--rm",
        "--memory=1g",
        "--network=bridge",
        "img-x",
        "/bin/nmap",
        "t",
    ]


def test_build_docker_argv_custom_docker_bin() -> None:
    """docker_bin 可指派（mama 场景 /usr/local/bin/docker），入呼叫序不吞。"""
    argv = build_docker_argv(
        ["/bin/nmap", "t"], image="secpt-sandbox", docker_bin="/usr/local/bin/docker"
    )
    assert argv[0] == "/usr/local/bin/docker"
    assert argv[1:3] == ["run", "--rm"]
    assert argv[7] == "secpt-sandbox"  # 四连 flags 占 argv[3:7]，image 紧随其后
    assert argv[8:] == ["/bin/nmap", "t"]


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
    """docker shim 收到的参数序 = run --rm *默认flags <image> <原始 argv>（一字不差）。"""
    docker_shim = _shim(tmp_path, "docker", 'echo "$@"')
    runner = DockerRunner(image="secpt-sandbox", docker_bin=docker_shim)
    proc = runner.run(["/bin/nmap-fake", "-Pn", "10.0.0.8"], timeout_s=5.0)
    assert proc.returncode == 0
    assert proc.stdout.split() == [
        "run",
        "--rm",
        *_DEFAULT_FLAGS,
        "secpt-sandbox",
        "/bin/nmap-fake",
        "-Pn",
        "10.0.0.8",
    ]


def test_docker_runner_flags_custom_passthrough(tmp_path: Path) -> None:
    """DockerRunner flags 注入面：装配层把 [execute].docker_flags 原样垫进呼叫序。"""
    docker_shim = _shim(tmp_path, "docker", 'echo "$@"')
    runner = DockerRunner(
        image="img-x", docker_bin=docker_shim, flags=["--memory=1g", "--network=none"]
    )
    proc = runner.run(["/bin/tool", "--flag"], timeout_s=5.0)
    assert proc.stdout.split() == [
        "run",
        "--rm",
        "--memory=1g",
        "--network=none",
        "img-x",
        "/bin/tool",
        "--flag",
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


def test_docker_raw_records_effective_command(tmp_path: Path) -> None:
    """raw 封面记真跑的命令：docker 包裹不得在账本上凭空消失（诚实锚）。"""
    docker_shim = _shim(tmp_path, "docker", 'echo "$@"')
    receipt = NmapTool(
        binary="/bin/nmap-fake",
        raw_dir=tmp_path / "raw",
        timeout_s=5.0,
        runner=DockerRunner(image="secpt-sandbox", docker_bin=docker_shim),
    ).run({"target": "10.0.0.9"})
    first_line = Path(receipt.raw_ref).read_text(encoding="utf-8").splitlines()[0]
    assert first_line == (
        f"$ argv({docker_shim} run --rm {' '.join(_DEFAULT_FLAGS)} "
        f"secpt-sandbox /bin/nmap-fake -Pn -sV 10.0.0.9)"
    )


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


# ---------- M4-5 加固配置（docker_flags / step_tool_limit） ------------------------


def test_hardening_defaults_when_missing(tmp_path: Path) -> None:
    """缺段/缺字段 = 四连加固 flags + 每步上限 4（M4-5 默认面，shell 档零影响）。"""
    assert DEFAULT_DOCKER_FLAGS == (
        "--network=none",
        "--memory=512m",
        "--read-only",
        "--cap-drop=ALL",
    )
    assert load_isolation_config(None).docker_flags == list(DEFAULT_DOCKER_FLAGS)
    assert load_isolation_config(None).step_tool_limit == 4
    bare = tmp_path / "profile.toml"
    bare.write_text("[execute]\nisolation = 'docker'\n", encoding="utf-8")
    cfg = load_isolation_config(str(bare))
    assert cfg.docker_flags == list(DEFAULT_DOCKER_FLAGS)
    assert cfg.step_tool_limit == 4


def test_hardening_section_loads(tmp_path: Path) -> None:
    """[execute] 段 docker_flags / step_tool_limit 各自入座（顺序 = 配置序）。"""
    path = tmp_path / "profile.toml"
    path.write_text(
        '[execute]\nisolation = "docker"\n'
        'docker_flags = ["--memory=1g", "--network=none"]\n'
        "step_tool_limit = 2\n",
        encoding="utf-8",
    )
    cfg = load_isolation_config(str(path))
    assert cfg.docker_flags == ["--memory=1g", "--network=none"]
    assert cfg.step_tool_limit == 2


def test_hardening_bad_values_reject(tmp_path: Path) -> None:
    """docker_flags 非列表 / step_tool_limit 非正整数 → E_PROFILE 拒跑不静默。"""
    bad_flags = tmp_path / "bad_flags.toml"
    bad_flags.write_text('[execute]\ndocker_flags = "--network=none"\n', encoding="utf-8")
    with pytest.raises(SecptError) as excinfo:
        load_isolation_config(str(bad_flags))
    assert excinfo.value.code == "E_PROFILE"

    bad_flag_item = tmp_path / "bad_item.toml"
    bad_flag_item.write_text("[execute]\ndocker_flags = [1, 2]\n", encoding="utf-8")
    with pytest.raises(SecptError) as excinfo:
        load_isolation_config(str(bad_flag_item))
    assert excinfo.value.code == "E_PROFILE"

    bad_limit = tmp_path / "bad_limit.toml"
    bad_limit.write_text("[execute]\nstep_tool_limit = 0\n", encoding="utf-8")
    with pytest.raises(SecptError) as excinfo:
        load_isolation_config(str(bad_limit))
    assert excinfo.value.code == "E_PROFILE"

    bad_limit_type = tmp_path / "bad_limit_type.toml"
    bad_limit_type.write_text("[execute]\nstep_tool_limit = 'four'\n", encoding="utf-8")
    with pytest.raises(SecptError) as excinfo:
        load_isolation_config(str(bad_limit_type))
    assert excinfo.value.code == "E_PROFILE"


# ---------- M4-5 真镜像无网集成（docker/镜像缺位则诚实 skip） ----------------------


def _docker_image_ready() -> bool:
    """docker 可用且 secpt-sandbox 镜像在位才跑真集成（离线 CI 诚实 skip 不虚红）。"""
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "image", "inspect", "secpt-sandbox"], capture_output=True, check=False
    )
    return probe.returncode == 0


class _HitCounter(http.server.BaseHTTPRequestHandler):
    """宿主靶服务：每被请求一次 hits+1——"宿主零请求"是宿主 shim 未跑的铁证。"""

    hits = 0

    def do_GET(self) -> None:  # noqa: N802  # http.server 基类手式（方法名必须大写）
        _HitCounter.hits += 1
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"secpt-host-ok")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """静默测试日志（父类会往 stderr 喷请求行）。"""
        return


@pytest.mark.skipif(not _docker_image_ready(), reason="docker 或 secpt-sandbox 镜像不可用")
def test_container_no_network_real_image() -> None:
    """容器加固可证（任务书验收 5）：--network=none 下容器内 curl 失败且宿主零请求。

    三段证据链：
    1. 经 DockerRunner（默认加固 flags）跑容器内 curl → 非零 rc（无网）；
    2. 宿主靶服务 hits==0——请求从未落到宿主（宿主 shim 未跑的对应铁证）；
    3. 对照组（同镜像、flags 摘除走 bridge）curl 成功且 hits==1——证明失败
       确实是 --network=none 掐的，不是镜像/回路残废导致的虚绿。
    对照组连不通的环境（无 docker0 网关 172.17.0.1）诚实 skip，不产假证据。
    """
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), _HitCounter)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gateway = "172.17.0.1"  # docker 默认网桥网关（对照组连不通则 skip）
    url = f"http://{gateway}:{port}/"
    try:
        hardened = DockerRunner(image="secpt-sandbox").run(
            ["curl", "-m", "3", "-sS", url], timeout_s=60.0
        )
        assert hardened.returncode != 0, f"加固容器内 curl 竟然成功：{hardened.stdout!r}"
        assert _HitCounter.hits == 0, "加固容器请求竟到达宿主服务——无网承诺被击穿"

        bridge_argv = build_docker_argv(
            ["curl", "-m", "3", "-sS", url], image="secpt-sandbox", flags=[]
        )
        control = subprocess.run(
            bridge_argv, capture_output=True, text=True, timeout=60.0, check=False
        )
        if control.returncode != 0:
            pytest.skip(f"环境不支持容器→宿主网关回路，对照组不成立：{control.stderr!r}")
        assert "secpt-host-ok" in control.stdout
        assert _HitCounter.hits >= 1
    finally:
        server.shutdown()
        server.server_close()
