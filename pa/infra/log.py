"""日志基建（stdlib logging，``secpt`` 命名空间）。

为什么用 stdlib 而不是 structlog：本工程约束第三方依赖只允许
pydantic/httpx/pyyaml（见 pa/envs.py 头注释），structlog 不在其中。
stdlib logging 的 %-style 延迟格式化 + 层级 logger 已覆盖需求；若未来
约束放开，get_logger 是唯一迁移点，调用方不需要改。

两个核心决策：

- 为什么只写 stderr、不落文件：events.jsonl 账本已经是「执行事实」的
  唯一事实源（append-only、可重放）——已进账本的事件（gateway
  blocked/finished、worker 失败）不再重复 log，日志只补「进不了账本
  的模块层静默降级」（配置损坏、一次降级被跳过的外部协作）。两份
  真相必然漂移，一份就够；stderr 流给运维捕获，账本给引擎重放。
- 为什么 handler 全部挂在 ``secpt`` 根 logger：logging 层级传播——
  业务模块只管 get_logger，配置一次全树生效；散落挂 handler 必然
  出现「有的名字被 configure 覆盖、有的没有」。

使用约定::

    from pa.infra.log import get_logger
    logger = get_logger(__name__)  # 统一归到 secpt.* 命名空间

幂等性：configure 重复调用不叠加 handler（重复打同一条警告比没有
日志更糟）；propagate=False 封闭在 ``secpt`` 树内，防止与 root
lastResort 双打。configure 只在 CLI 入口（pa.cli.main）显式调用；
测试捕获 emission 时不用 configure——裸 logger 向 root 传播，
pytest caplog 直接能抓到。
"""

from __future__ import annotations

import logging
import sys

_ROOT = "secpt"
_FMT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATEFMT = "%H:%M:%S"


def _normalize(name: str) -> str:
    """把任意模块名归到 ``secpt`` 命名空间（强制前缀，保证 configure
    一次挂 handler 就覆盖全部业务 logger；散落命名会让 handler 挂空）。"""
    root = f"{_ROOT}."
    return name if name == _ROOT or name.startswith(root) else f"{root}{name}"


def get_logger(name: str) -> logging.Logger:
    """业务统一取 logger 入口：保证 ``secpt.*`` 命名。"""
    return logging.getLogger(_normalize(name))


def configure(level: str = "WARNING") -> None:
    """幂等配置：往 ``secpt`` 根 logger 挂 stderr handler；重复调用不叠加。

    为什么 propagate=False：现在 handler 挂在 ``secpt`` 上，消息如果再
    向 root 传播、而 root 又有 handler（lastResort 或测试基建），同一
    行会打两遍。挂了 handler 的树必须在此封闭。
    """
    secpt = logging.getLogger(_ROOT)
    if not secpt.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
        secpt.addHandler(h)
        secpt.propagate = False
    secpt.setLevel(level.upper())
