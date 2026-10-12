"""执行沙箱：把引擎的 bash 执行放进 Docker 容器（宿主隔离）。

模式：
- ``auto``（默认）：docker 可用则用容器，不可用静默回退宿主执行
- ``docker``：强制容器（不可用时 bash 工具返回 Error，任务可感知）
- ``none``：宿主执行（现状）

每个 job 一个常驻容器（``docker run -d sleep infinity`` + ``docker exec``），
避免每次命令的容器启动开销；任务结束自动销毁。工作目录以 volume 挂载到
容器的 /work，与宿主 ws 完全同步（write_file 产物在容器内立即可见）。

配置（环境变量）：
- SECPT_SANDBOX=0        全局关闭（等价 --no-sandbox）
- SECPT_SANDBOX_IMAGE    镜像名（默认 secpt-sandbox；未构建时回退 debian:bookworm-slim
  + 警告——工具可能缺 nmap 等）
- SECPT_SANDBOX_NETWORK  网络模式（默认 bridge；内网靶场可设 host）
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path

DEFAULT_IMAGE = "secpt-sandbox"
FALLBACK_IMAGE = "debian:bookworm-slim"
CONTAINER_TTL_S = 4 * 3600  # 容器兜底自毁（job 异常退出时泄漏防护）


def docker_available() -> bool:
    if os.environ.get("SECPT_SANDBOX") == "0":
        return False
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class DockerSandbox:
    """单 job 的常驻执行容器。best-effort：创建失败抛出 SandboxUnavailable。"""

    def __init__(
        self, job_id: str, *, work_dir: str, image: str | None = None, network: str | None = None
    ) -> None:
        self.job_id = job_id
        self.work_dir = work_dir
        self.name = f"secpt-{job_id}-{uuid.uuid4().hex[:6]}"
        self.network = network or os.environ.get("SECPT_SANDBOX_NETWORK", "bridge")
        img = image or os.environ.get("SECPT_SANDBOX_IMAGE", DEFAULT_IMAGE)
        if self._image_exists(img):
            self.image = img
        else:
            print(
                f"[sandbox] 镜像 {img} 未构建，回退 {FALLBACK_IMAGE}"
                "（缺 nmap/whatweb 等；make sandbox-image 可构建完整环境）"
            )
            self.image = FALLBACK_IMAGE

    @staticmethod
    def _image_exists(img: str) -> bool:
        try:
            r = subprocess.run(["docker", "image", "inspect", img], capture_output=True, timeout=15)
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def start(self) -> None:
        # 关键：挂载源必须预先由本进程创建——docker 守护进程会为不存在的 bind 源
        # 自动创建 root:root 目录（容器内 uid 不符 → PermissionError）
        Path(self.work_dir).mkdir(parents=True, exist_ok=True)
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            self.name,
            "--network",
            self.network,
            # 以宿主 uid 运行：bind mount 两侧 uid 一致，文件归属不打架
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{self.work_dir}:/work",
            "-w",
            "/work",
            "--rm",  # stop 后自动清理
            self.image,
            "sleep",
            str(CONTAINER_TTL_S),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise SandboxUnavailable(f"容器启动失败: {r.stderr.strip()[:200]}")
        # 等容器就绪
        for _ in range(20):
            chk = subprocess.run(
                ["docker", "exec", self.name, "true"], capture_output=True, timeout=5
            )
            if chk.returncode == 0:
                return
            time.sleep(0.3)
        raise SandboxUnavailable("容器就绪超时")

    def exec(
        self, argv: list[str], *, timeout_s: int, env: dict[str, str] | None = None
    ) -> tuple[int, str, str]:
        """在容器内执行 argv（数组形式，不经 shell）。返回 (rc, stdout, stderr)。"""
        cmd = ["docker", "exec"]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [self.name, *argv]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s + 5, errors="replace"
            )
        except subprocess.TimeoutExpired:
            return 124, "", "timeout"
        return r.returncode, r.stdout, r.stderr

    def stop(self) -> None:
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, timeout=20)

    def __enter__(self) -> DockerSandbox:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


class SandboxUnavailable(RuntimeError):
    """沙箱不可用（docker 缺失/启动失败）。"""


def sandbox_scope(job_id: str, *, work_dir: str, mode: str = "auto"):
    """上下文管理器：mode=docker/auto 且有 docker → 容器执行；否则 None（宿主）。

    用法（engine run_bash 内）：
        with sandbox_scope(...) as sb:
            if sb: rc,out,err = sb.exec(argv, timeout_s=...)
            else:  宿主 subprocess
    """
    from contextlib import contextmanager

    # 环境变量优先级：SECPT_SANDBOX=0 全局关闭；SECPT_SANDBOX_MODE 强制模式
    mode = os.environ.get("SECPT_SANDBOX_MODE") or mode

    @contextmanager
    def _ctx():
        if mode == "none" or os.environ.get("SECPT_SANDBOX") == "0":
            yield None
            return
        if not docker_available():
            if mode == "docker":
                raise SandboxUnavailable("docker 不可用但强制容器模式（SECPT_SANDBOX_MODE=docker）")
            yield None
            return
        sb = DockerSandbox(job_id, work_dir=work_dir)
        try:
            sb.start()
            yield sb
        finally:
            sb.stop()

    return _ctx()


def quote_argv(argv: list[str]) -> str:
    """宿主 shlex 解析后的 argv 原样传给容器 exec（无需再拼命令串）。"""
    return " ".join(shlex.quote(a) for a in argv)
