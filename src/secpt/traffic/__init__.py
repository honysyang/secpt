"""流量证据层：MITM 透明录制（Phase B3）。

TrafficRecorder 按 job 启动 mitmproxy 录制代理；ProxyHandle 向 os.environ
注入 SECPT_PROXY_URL / SECPT_PROXY_CA（engine 子进程自动继承）；
TrafficStore 把 HTTP 交换落 SQLite 并提供 search/get/stats。
"""

from secpt.traffic.recorder import ProxyHandle, TrafficRecorder
from secpt.traffic.store import TrafficStore, record_exchange

__all__ = ["ProxyHandle", "TrafficRecorder", "TrafficStore", "record_exchange"]
