"""runtime 包：主循环 / 调度器 / 预算 / 租约 / 恢复（ARCHITECTURE §8 L4）。

对上的唯一入口是 run()：引导与恢复合一，调用方不需要知道目录新旧。
"""

from pa.runtime.loop import run
from pa.runtime.recovery import resume

__all__ = ["run", "resume"]
