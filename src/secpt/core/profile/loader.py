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

# 全局提效约束（engine_runner 装配 constraints 时统一追加，不逐 profile 重复）：
# 引擎每步已支持并行执行多个 tool_calls（_PARALLEL_MAX=4），瓶颈是模型习惯一轮
# 只发一个——每多发一个 tool_call 省一次模型往返（单步 5-15s，步数是延迟大头）。
PROFILE_BATCH_CONSTRAINT = (
    "一轮多个动作：相互独立的操作（并行探测多个路径/读多个文件/跑互不依赖的命令）"
    "在同一轮 assistant 消息里一次发出多个 tool_calls，引擎会并行执行；"
    "只有存在依赖关系（下一步的参数来自上一步的结果）才拆到下一轮。"
)


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


class RoeSpec(BaseModel):
    """交战规则（Rules of Engagement）：双模治理。

    - mode "ctf"（默认）：竞技/实验场景，无额外限速，行为边界靠 profile.constraints
    - mode "authorized"：授权实战场景——引擎每个 step 之间节速 throttle_ms，
      solve 打印 ROE 横幅（操作留痕提示），constraints 追加进系统提示
    """

    mode: Literal["ctf", "authorized"] = "ctf"
    throttle_ms: int = 0  # 每个引擎 step 后的节速 sleep（毫秒），authorized 实战建议 >0


class Stage(BaseModel):
    """多阶段渗透的一个阶段（profile.stages；非空 → 折叠为 frontier 线性意图链）"""

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
    # 多标签合并的先验并集（理解智能体横向任务产物）：prior_text() 按序串联
    prior_extra: list[str] = Field(default_factory=list)
    terminator: Terminator = Field(default_factory=Terminator)
    budget: Budget = Field(default_factory=Budget)
    stages: list[Stage] = Field(default_factory=list)  # 非空 → 折叠为 frontier 线性意图链
    workers: int = 1  # S6 模式收敛：frontier 并发 worker 数（多资产实战场景 3-4）
    traffic: bool = False  # true → 任务级 MITM 流量录制（B3）
    sandbox_image: str = ""  # 非空 → 本 profile 的沙箱镜像（如 secpt-sandbox-pwn）
    bash_timeout_s: int = 0  # 非 0 → 覆盖引擎 bash 工具超时（如 pwn 的 QEMU 长交互）
    stuck_wait_cap: int = 0  # 非 0 → 覆盖卡死检测 waiting 上限（pwn 长时交互调大）
    constraints: list[str] = Field(default_factory=list)
    roe: RoeSpec = Field(default_factory=RoeSpec)  # 交战规则（双模治理）
    note: str = ""
    sandbox: str = ""  # 非空 → 覆盖全局 sandbox 默认（none）；需容器时写 docker/auto

    @property
    def prior_path(self) -> Path:
        return PRIORS_DIR / self.prior

    def prior_text(self) -> str:
        """先验文本（含多标签合并的 prior_extra 串联；缺失文件跳过，去重）。"""
        parts: list[str] = []
        seen: set[str] = set()
        for fn in [self.prior, *self.prior_extra]:
            if not fn or fn in seen:
                continue
            seen.add(fn)
            fp = PRIORS_DIR / fn
            if fp.is_file():
                parts.append(fp.read_text(encoding="utf-8").strip())
        return "\n\n".join(parts)

    def merged_whitelist_extra(self) -> list[str]:
        """tools 里非内置项 + 显式 bash_whitelist_extra 的去重合并。"""
        seen: list[str] = []
        for t in self.tools + self.bash_whitelist_extra:
            if t in CORE_TOOLS or t in seen:
                continue
            seen.append(t)
        return seen


def load_profile(name: str) -> Profile:
    """加载 profile YAML。支持两类入参（多标签合并物化后按路径加载）：

    - 内置名：``ctf`` → ``profiles/ctf.yaml``；找不到回退默认值（兼容旧名）
    - YAML 路径：``/…/merged-<job>.yaml`` 或含 ``/`` 的字符串 → 直接读该文件
    """
    is_path = name.endswith((".yaml", ".yml")) or "/" in name
    path = Path(name) if is_path else PROFILES_DIR / f"{name}.yaml"
    fallback = Path(name).stem if is_path else name
    if not path.is_file():
        return Profile(name=fallback)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return Profile(name=fallback)
    data.setdefault("name", fallback)
    return Profile(**data)
