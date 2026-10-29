"""执行隔离（M3-5 拍板②）：runner seam + Docker 包裹 + profile [execute] 装配。

为什么 seam 在 NmapTool 构造器而不是 dispatch 层：argv 构建属工具自洽（工具知道
怎么调自己），执行方式属装配决策（宿主跑还是容器跑）——两层各司其职，测试可
对 saw 缝独立注入（tests/unit/test_execute_isolation.py）。

fail-closed 纪律（拍板②）：isolation="docker" 时 docker bin/镜像缺失一律
E_TOOL_FAIL 回执、绝不静默退回本机执行——宿主方案不可信 = 拒绝比兜底诚实；
NmapTool 管"转回执"，本层只管"如实失败"（FileNotFoundError 原样上抛）。

build_docker_argv 是纯函数：无 subprocess、无副作用，argv 序 = 手写固定——
原始 argv 原样垫底（[docker, run, --rm, image, *argv]），容器内看到的就是
宿主本来要跑的那串命令，一字不差。
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel

from secpt.errors import SecptError


class ArgvRunner(Protocol):
    """执行器契约：吃 argv 列表，吐 CompletedProcess；失败语义上抛由工具转回执。

    ``command`` 是"实际要跑的那串命令"的投影（raw 落盘用）：LocalRunner 原样返回，
    DockerRunner 返回包裹后的 argv——账本封面记的必须是真跑过的东西，
    不能让 docker 包裹在 raw 里凭空消失。
    """

    def command(self, argv: list[str]) -> list[str]:
        """返回该执行器实际交给操作系统的 argv（纯函数，不执行）。"""
        ...

    def run(self, argv: list[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
        """同步执行一次：shell 例如 subprocess 语义，docker 则 Wrapped。"""
        ...


class LocalRunner:
    """宿主执行器：与 M1/M2 的 subprocess.run 行为逐字节守恒（拍板② 缺省档）。"""

    label = "shell"

    def command(self, argv: list[str]) -> list[str]:
        """本机执行不做任何包裹：实际 argv 就是工具构建的 argv。"""
        return list(argv)

    def run(self, argv: list[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
        """argv 列表直接进 subprocess（禁 shell=True 在工具层纪律，本层不改写）。"""
        return subprocess.run(  # noqa: S603  # argv 列表，无 shell
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )


def build_docker_argv(argv: list[str], *, image: str, docker_bin: str = "docker") -> list[str]:
    """把宿主 argv 包成 [docker_bin, run, --rm, image, *argv]（纯函数）。"""
    return [docker_bin, "run", "--rm", image, *argv]


class DockerRunner:
    """容器执行器：argv 包裹后仍走 subprocess（exec 语义与 shell 同械）。"""

    def __init__(self, image: str, docker_bin: str = "docker") -> None:
        self._image = image
        self._docker_bin = docker_bin

    @property
    def label(self) -> str:
        """执行器身份一句话（失败回执署名用——"docker" 照何进回执）。"""
        return "docker"

    def command(self, argv: list[str]) -> list[str]:
        """实际命令 = docker 包裹后的完整 argv（raw 落盘照此署名）。"""
        return build_docker_argv(argv, image=self._image, docker_bin=self._docker_bin)

    def run(self, argv: list[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
        """包裹一次：纯函数定 argv 序 → subprocess 照跑（缺 bin/镜像 = FileNotFoundError/诚
        实 rc 透传）。无 runner 侧 fallback，NmapTool 落 E_TOOL_FAIL。"""
        wrapped = self.command(argv)
        return subprocess.run(  # noqa: S603  # argv 列表，无 shell
            wrapped, capture_output=True, text=True, timeout=timeout_s, check=False
        )


class IsolationConfig(BaseModel):
    """[execute] 段装配快照：isolation 档位 / 镜像 / docker bin 三字段。"""

    isolation: Literal["shell", "docker"] = "shell"
    image: str = "secpt-sandbox"
    docker_bin: str = "docker"


def load_isolation_config(path: str | None) -> IsolationConfig:
    """读 profile.toml [execute] 段装配 IsolationConfig（三档语义）。

    - path 为 None / 文件不存在 / 缺 [execute] 段 → shell 档默认值
      （isolation 不开，M1/M2 行为逐字节守恒）；
    - toml 解析失败 / isolation 值不合法 → SecptError(E_PROFILE)（笔误拒跑不静默，
      同 judge/guard 手式）；
    - [execute] 段存在 → 三字段各自入座（pydantic Literal 兜底校验）。
    """
    if not path:
        return IsolationConfig()
    file = Path(path)
    if not file.is_file():
        return IsolationConfig()
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, ValueError) as exc:
        raise SecptError(
            "profile.toml 解析失败",
            code="E_PROFILE",
            context={"path": str(file), "reason": str(exc)},
        ) from exc
    section = data.get("execute")
    if not isinstance(section, dict):
        return IsolationConfig()
    raw_isolation = section.get("isolation", "shell")
    raw_image = section.get("image", "secpt-sandbox")
    raw_docker_bin = section.get("docker_bin", "docker")
    if not isinstance(raw_isolation, str) or raw_isolation not in ("shell", "docker"):
        raise SecptError(
            '[execute].isolation 必须是 "shell" 或 "docker"',
            code="E_PROFILE",
            context={"path": str(file), "got": str(raw_isolation)},
        )
    if not isinstance(raw_image, str) or not isinstance(raw_docker_bin, str):
        raise SecptError(
            "[execute].image / [execute].docker_bin 必须是字符串",
            code="E_PROFILE",
            context={"path": str(file)},
        )
    return IsolationConfig(
        isolation=raw_isolation,  # type: ignore[arg-type]  # 已人工收紧到字面量
        image=raw_image,
        docker_bin=raw_docker_bin,
    )


__all__ = [
    "ArgvRunner",
    "DockerRunner",
    "IsolationConfig",
    "LocalRunner",
    "build_docker_argv",
    "load_isolation_config",
]
