"""通知通道：审批待办 / 任务暂停 / 拿到 flag → webhook + 桌面通知。

环境变量（都不配 = 静默跳过；通知是观测层，绝不阻断主链路）：
- SECPT_NOTIFY_WEBHOOK   URL：POST JSON {event,title,body,ts}（飞书/Slack/钉钉
  机器人 webhook 与自建网关均可用）
- SECPT_NOTIFY_DESKTOP   1 → notify-send（Linux 桌面气泡）

实现口径：发送在守护线程里跑（hook 点多数在同步路径，不能阻塞主链路）；
任何失败静默（best-effort，与 learning/治理观测层同哲学）。
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
import time

_WEBHOOK_TIMEOUT_S = 5.0
_DESKTOP_TIMEOUT_S = 5.0


def configured() -> bool:
    """是否配置了任一通知通道（未配置时 notify 直接返回，零开销）。"""
    return bool(os.environ.get("SECPT_NOTIFY_WEBHOOK")) or (
        os.environ.get("SECPT_NOTIFY_DESKTOP", "") == "1"
    )


def notify(event: str, title: str, body: str = "") -> None:
    """异步发一条通知（守护线程；未配置通道时直接返回）。"""
    webhook = os.environ.get("SECPT_NOTIFY_WEBHOOK", "")
    desktop = os.environ.get("SECPT_NOTIFY_DESKTOP", "") == "1"
    if not webhook and not desktop:
        return
    threading.Thread(
        target=_send,
        args=(event, title, body, webhook, desktop),
        daemon=True,
        name="secpt-notify",
    ).start()


def _send(event: str, title: str, body: str, webhook: str, desktop: bool) -> None:
    """同步发送（线程体内；测试直接调用本函数避开线程时序）。"""
    if webhook:
        try:
            import urllib.request

            payload = json.dumps(
                {"event": event, "title": title, "body": body, "ts": time.time()},
                ensure_ascii=False,
            ).encode("utf-8")
            req = urllib.request.Request(
                webhook, data=payload, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=_WEBHOOK_TIMEOUT_S):
                pass
        except Exception:
            pass  # 通知失败静默（不许反噬主链路）
    if desktop:
        with contextlib.suppress(Exception):
            subprocess.run(
                ["notify-send", title, body or event],
                capture_output=True,
                timeout=_DESKTOP_TIMEOUT_S,
            )
