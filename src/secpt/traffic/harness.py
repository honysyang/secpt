"""流量录制编排：run_engine/worker 的"启动→注入 env→收尾"统一入口。

用法：
    with traffic_scope(job_id, enabled=True) as handle:
        ... 跑 Engine（bash 子进程自动经代理被录制） ...
    # 退出时自动 clear_env + stop

enabled=False 或启动失败 → 静默跳过（录制是观测层，绝不阻断主链路）。
"""

from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def traffic_scope(job_id: str, *, enabled: bool = False):
    """enabled 时启动 MITM 录制并注入 SECPT_PROXY_URL/CA；退出时清理。"""
    if not enabled:
        yield None
        return
    handle = None
    try:
        from secpt.traffic.recorder import TrafficRecorder

        handle = TrafficRecorder.start(job_id)
        if handle is not None:
            handle.set_env()
            print(f"[traffic] 录制中: {handle.url}（job={job_id}）")
    except Exception as e:
        print(f"[traffic] 启动失败（忽略，不录制）: {e}")
        handle = None
    try:
        yield handle
    finally:
        if handle is not None:
            try:
                handle.clear_env()
                handle.stop()
            except Exception as e:
                print(f"[traffic] 收尾失败（忽略）: {e}")
