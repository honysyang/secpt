"""S2 测试用假 LLM：worker 在 SECPT_FAKE_LLM=1 时使用。

每轮调用一次 bash（ls），n 轮后输出 ANSWER: FLAG{...}（内容含 job_id）。
S3：content_len>0 时给每步内容补足长度（假 LLM 也要能长上下文触发压缩/轮转）。
"""

from __future__ import annotations

import asyncio
import json


class FakeLLM:
    def __init__(
        self,
        n_steps: int = 4,
        job_id: str = "fake",
        step_delay_s: float = 0.3,
        content_len: int = 0,
    ) -> None:
        self.n = max(1, n_steps)
        self.job_id = job_id
        self.calls = 0
        self.step_delay_s = step_delay_s
        self.content_len = max(0, int(content_len))

    async def call(self, messages, tools, model):
        if self.step_delay_s:
            await asyncio.sleep(self.step_delay_s)
        # 指挥官受理轮（接战确认闸）：返回 canned《接战单》，fake 全链路也能演示确认闸。
        # 提示词含"接战受理"即判定（不消耗 fake 步数）。
        _prompt0 = str((messages[0].get("content") if messages else "") or "")
        if "接战受理" in _prompt0:
            return _resp(
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "goal": {
                                "objective": f"fake 受理目标（{self.job_id}）",
                                "restatement": "fake：拿下目标站并拿到 flag",
                                "success_criteria": ["拿到 FLAG{...}"],
                                "constraints": ["授权范围内"],
                                "asset_scope": [],
                                "attack_steps": ["侦察", "利用"],
                            },
                            "risk_notes": [],
                        },
                        ensure_ascii=False,
                    ),
                }
            )
        self.calls += 1
        if self.calls > self.n:
            return _resp(
                {
                    "role": "assistant",
                    "content": f"ANSWER: FLAG{{{self.job_id}_done}}",
                }
            )
        body = f"fake step {self.calls}"
        if self.content_len > len(body):
            body += " x" * (self.content_len - len(body))
        return _resp(
            {
                "role": "assistant",
                "content": body,
                "tool_calls": [
                    {
                        "id": f"fake-{self.job_id}-{self.calls}",
                        "type": "function",
                        "function": {"name": "bash", "arguments": json.dumps({"command": "ls"})},
                    }
                ],
            }
        )


def _resp(msg_dict: dict):
    class _Fn:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    class _TC:
        def __init__(self, tc):
            self.id = tc["id"]
            self.type = tc.get("type", "function")
            self.function = _Fn(tc["function"]["name"], tc["function"]["arguments"])

    class _Msg:
        def __init__(self, d):
            self.content = d.get("content") or ""
            self.tool_calls = [_TC(x) for x in (d.get("tool_calls") or [])]
            self.reasoning_content = None

    class _Choice:
        def __init__(self, msg):
            self.message = msg

    class _Resp:
        def __init__(self, msg):
            self.choices = [_Choice(msg)]

    return _Resp(_Msg(msg_dict))
