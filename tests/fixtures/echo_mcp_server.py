"""本地 echo MCP server（M4-2 测试/演示桩）：stdio JSON-RPC 最小实现。

只实现 MCP 三招：initialize / tools/list / tools/call（echo 回声工具）。
按行读 JSON-RPC 请求，逐行回响应；notification（无 id）只吞不回。
``__crash__`` 魔法文本用于"server 半途死亡"失败注入（stdin/stdout 一起断）。
"""

from __future__ import annotations

import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "原样回声输入文本（演示桩）",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "times": {"type": "integer"}},
            "required": ["text"],
        },
    }
]


def _respond(rid: int, result: dict[str, object]) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        method = req.get("method")
        rid = req.get("id")
        if rid is None:  # notification：只吞不回
            continue
        if method == "initialize":
            _respond(
                rid,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "echo", "version": "0.1.0"},
                },
            )
        elif method == "tools/list":
            _respond(rid, {"tools": TOOLS})
        elif method == "tools/call":
            args = req.get("params", {}).get("arguments", {})
            text = str(args.get("text", ""))
            if text == "__crash__":
                sys.exit(3)  # 半途死亡：不回响应直接断（失败注入口）
            times = max(1, int(args.get("times", 1)))
            _respond(
                rid,
                {"content": [{"type": "text", "text": text * times}], "isError": False},
            )
        else:
            sys.stdout.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32601, "message": f"method not found: {method}"},
                    }
                )
                + "\n"
            )
            sys.stdout.flush()


if __name__ == "__main__":
    main()
