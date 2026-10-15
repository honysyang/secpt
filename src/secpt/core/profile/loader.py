"""S4 Profile 加载：YAML → Profile dataclass。

核心原则：异构用数据表达，不用代码表达。五个 profile = 五个 YAML，零个新执行器。
tools 列表里除 Engine 内置四工具（bash/read_file/list_dir/write_file）之外的
名字一律视为"允许通过 bash 执行的命令"，并入 bash 白名单（数据驱动）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

PROFILES_DIR = Path(__file__).resolve().parent / "profiles"
PRIORS_DIR = Path(__file__).resolve().parents[2] / "knowledge" / "priors"

CORE_TOOLS = frozenset({"bash", "read_file", "list_dir", "write_file"})

DEFAULT_ROLE = "渗透测试专家，正在解一道 CTF"
DEFAULT_CONSTRAINTS = ["不要扫描题面外的网段。"]


class Terminator(BaseModel):
    """终止条件：flag 类靠 flag_regex；budget 类靠预算 + stop_condition 提示。"""

    kind: Literal["flag", "budget"] = "flag"
    flag_regex: str | None = None
    stop_condition: str = ""


class Budget(BaseModel):
    steps: int = 100
    wall_s: float = 900.0
    rounds: int = 0
    cost_cny: float = 0.0


class Stage(BaseModel):
    """多阶段渗透的一个阶段（profile.stages；空则走单链）"""

    name: str
    role: str = ""
    prior: str = ""  # 留空继承 profile.prior
    tools: list[str] = Field(default_factory=list)  # 插件工具名（已装插件）
    stop_condition: str = ""  # 该阶段收尾方式（注入约束）
    budget: Budget = Field(default_factory=Budget)  # steps/wall_s=0 用 profile.budget


class Profile(BaseModel):
    name: str
    role: str = DEFAULT_ROLE
    tools: list[str] = Field(default_factory=lambda: list(CORE_TOOLS))
    bash_whitelist_extra: list[str] = Field(default_factory=list)
    prior: str = "generic.md"  # knowledge/priors/<prior>，相对文件名
    terminator: Terminator = Field(default_factory=Terminator)
    budget: Budget = Field(default_factory=Budget)
    stages: list[Stage] = Field(default_factory=list)  # 非空 → 多阶段编排
    traffic: bool = False  # true → 任务级 MITM 流量录制（B3）
    sandbox_image: str = ""  # 非空 → 本 profile 的沙箱镜像（如 secpt-sandbox-pwn）
    bash_timeout_s: int = 0  # 非 0 → 覆盖引擎 bash 工具超时（如 pwn 的 QEMU 长交互）
    stuck_wait_cap: int = 0  # 非 0 → 覆盖卡死检测 waiting 上限（pwn 长时交互调大）
    constraints: list[str] = Field(default_factory=list)
    note: str = ""
    sandbox: str = ""  # 非空 → 覆盖全局 sandbox 默认（none）；需容器时写 docker/auto

    @property
    def prior_path(self) -> Path:
        return PRIORS_DIR / self.prior

    def prior_text(self) -> str:
        if not self.prior_path.is_file():
            return ""
        return self.prior_path.read_text(encoding="utf-8").strip()

    def merged_whitelist_extra(self) -> list[str]:
        """tools 里非内置项 + 显式 bash_whitelist_extra 的去重合并。"""
        seen: list[str] = []
        for t in self.tools + self.bash_whitelist_extra:
            if t in CORE_TOOLS or t in seen:
                continue
            seen.append(t)
        return seen


def load_profile(name: str) -> Profile:
    """加载 profile YAML；找不到文件时回退默认值（兼容 generic 等旧名）。"""
    path = PROFILES_DIR / f"{name}.yaml"
    if not path.is_file():
        return Profile(name=name)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return Profile(name=name)
    data.setdefault("name", name)
    return Profile(**data)
