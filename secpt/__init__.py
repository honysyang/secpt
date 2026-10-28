"""secpt —— 渗透测试智能体 harness。

设计定位：薄骨架 + 可插拔器官（FSM 管状态、黑板管证据、Guard 管动作、verify 管真伪）。
分层依赖红线：core → llms → tools/store → judge → finalize，单向；禁止旁路绕过 store 直写 sqlite。
"""

__version__ = "0.1.0"
