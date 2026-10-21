"""父事件流（var/jobs/<id>/events.jsonl）的跨写入方串行锁。

事件流有三个写入方，各自的原子锁互不知情：
- 编排器 `_parent_event`（frontier.py，编排线程）
- EventSink.write（web/api.py 追问注入等，HTTP 工作线程；engine 子任务线程）
- 聚合器 `_aggregate_sub_events`（编排线程，final 补漏）

seq 都是"先数行/读尾 seq → 再 append"，两步之间另一写入方插入就会撞重号/
交错行（实测面板偶发 seq 重号）。统一在此持锁：任一写入方写父流时全局串行。
锁内不做 IO 以外的事，持有时间微秒级，不成为吞吐瓶颈。
"""

from __future__ import annotations

import threading

PEV_LOCK = threading.Lock()
