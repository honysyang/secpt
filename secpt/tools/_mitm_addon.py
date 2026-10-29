"""mitmdump addon（M4-3）：把每个 HTTP 响应钩成一条 JSONL 流量记录。

为什么走 addon 而不是读 mitmproxy 的 flow dump 文件：JSONL 逐条追加、逐条
落盘（open/append/close），stop 时按行解析即可交割，不依赖 mitmproxy 自身
序列化格式的版本兼容；进程被 SIGTERM 时已写行不丢。

分层红线：本文件由 mitmdump 子进程以独立解释器加载（``-s`` 注入路径），
secpt 主进程永不 import 它——mitmproxy 依赖不进主进程依赖树。输出路径经
环境变量 SECPT_MITMDUMP_FLOW_OUT 注入（MitmCapture.start 设置），addon
自身零配置、无状态。
"""

from __future__ import annotations

import json
import os

from mitmproxy import http

# 单侧 body 截断上限：证据保真与快照体积的折中（64KiB 覆盖演示流量绰绰有余）。
_BODY_LIMIT = 65536

_OUT = os.environ.get("SECPT_MITMDUMP_FLOW_OUT", "")


def response(flow: http.HTTPFlow) -> None:
    """response 钩子：每条响应追加一行 JSON（缺输出路径时静默跳过）。"""
    if not _OUT:
        return
    request_body = (flow.request.raw_content or b"")[:_BODY_LIMIT].decode("utf-8", errors="replace")
    response_body = (
        (flow.response.raw_content or b"")[:_BODY_LIMIT].decode("utf-8", errors="replace")
        if flow.response
        else ""
    )
    record = {
        "method": flow.request.method,
        "url": flow.request.pretty_url,
        "status": flow.response.status_code if flow.response else 0,
        "request": request_body,
        "response": response_body,
    }
    with open(_OUT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
