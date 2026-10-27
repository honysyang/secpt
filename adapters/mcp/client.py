"""MCP 协议适配器：接入外部工具服务。

Model Context Protocol (MCP) 是 Anthropic 推出的开放协议，
用于标准化 LLM 与外部工具/数据源的交互。

本模块提供：
- MCP 客户端：连接外部 MCP 服务器
- 工具转换：将 MCP 工具转换为内部 Tool 格式
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any


@dataclass
class MCPTool:
    """MCP 工具定义。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    server: str  # 来源服务器


class MCPClient:
    """MCP 客户端：连接外部工具服务器。"""

    def __init__(self, server_url: str, name: str = "mcp_server") -> None:
        self.server_url = server_url
        self.name = name
        self._process: asyncio.subprocess.Process | None = None
        self._tools: list[MCPTool] = []

    async def connect(self) -> None:
        """连接到 MCP 服务器（stdio 模式）。

        生产实现应该使用 MCP SDK，这里简化为子进程模式。
        """
        # 实际实现应该使用 mcp SDK
        # 这里提供接口占位
        pass

    async def list_tools(self) -> list[MCPTool]:
        """列出服务器提供的工具。"""
        # 实际实现应该调用 MCP 协议
        return self._tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """调用远程工具。"""
        # 实际实现应该通过 MCP 协议调用
        pass

    async def disconnect(self) -> None:
        """断开连接。"""
        if self._process:
            self._process.terminate()
            await self._process.wait()
            self._process = None


class MCPToolAdapter:
    """MCP 工具适配器：将 MCP 工具转换为内部 Tool 格式。"""

    def __init__(self, client: MCPClient) -> None:
        self.client = client

    async def convert_tools(self) -> list[dict[str, Any]]:
        """将 MCP 工具转换为内部 Schema。"""
        mcp_tools = await self.client.list_tools()

        return [
            {
                "type": "function",
                "function": {
                    "name": f"{self.client.name}_{tool.name}",
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in mcp_tools
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """执行 MCP 工具。

        Args:
            name: 工具名（格式: {server}_{tool}）
            arguments: 工具参数

        Returns:
            工具执行结果
        """
        # 去掉服务器前缀
        tool_name = name.replace(f"{self.client.name}_", "")
        return await self.client.call_tool(tool_name, arguments)


# 常用 MCP 服务器配置示例
MCP_SERVER_PRESETS = {
    "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/files"],
        "description": "文件系统访问",
    },
    "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_TOKEN": ""},
        "description": "GitHub 操作",
    },
    "sqlite": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sqlite", "/path/to/db"],
        "description": "SQLite 数据库",
    },
    "web_search": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-brave-search"],
        "env": {"BRAVE_API_KEY": ""},
        "description": "网页搜索",
    },
}
