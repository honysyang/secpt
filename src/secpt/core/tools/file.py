"""文件系统工具：读题目文件、列目录。"""

from __future__ import annotations

from pathlib import Path

from secpt.core.tools.base import Tool


class ReadFileTool(Tool):
    """读取本地文件内容。"""

    name = "read_file"
    description = "读取本地文件，如题目描述 test.md"

    # 后缀白名单：只允许文本类文件
    ALLOWED_SUFFIXES = {".md", ".txt", ".yaml", ".yml", ".json", ".conf", ".py", ".log"}
    MAX_FILE_SIZE = 200 * 1024  # 200KB

    async def run(self, args: str) -> str:
        path = args.strip()
        if not path:
            return "Error: 用法: read_file <path>"

        try:
            # 限制在常见目录内
            allowed_prefixes = ["/home/kali/challenges", "/home/kali/secpt", "/tmp"]
            resolved = str(Path(path).resolve())
            if not any(resolved.startswith(p) for p in allowed_prefixes):
                return f"Error: 路径不允许: {path}"

            p = Path(resolved)
            if not p.is_file():
                return f"Error: 不是文件: {path}"

            # 后缀白名单
            if p.suffix.lower() not in self.ALLOWED_SUFFIXES:
                return f"Error: 不允许读取该类型文件（后缀白名单: {sorted(self.ALLOWED_SUFFIXES)}）: {path}"

            # 大小上限
            file_size = p.stat().st_size
            if file_size > self.MAX_FILE_SIZE:
                return f"Error: 文件过大（{file_size} bytes > {self.MAX_FILE_SIZE} bytes 上限）: {path}"

            content = p.read_text(encoding="utf-8", errors="replace")
            # 截断防 OOM
            if len(content) > 10000:
                content = content[:10000] + "\n... [truncated]"
            return content
        except FileNotFoundError:
            return f"Error: 文件不存在: {path}"
        except PermissionError:
            return f"Error: 权限不足: {path}"
        except Exception as e:
            return f"Error: {e}"


class ListDirTool(Tool):
    """列出目录内容（深度 1 层），输出文件名 + 大小。"""

    name = "list_dir"
    description = "列出目录内容（深度1层），输出文件名+大小，如 list_dir /home/kali/challenges/rois-saml-sso"

    MAX_OUTPUT = 3000

    async def run(self, args: str) -> str:
        path = args.strip()
        if not path:
            return "Error: 用法: list_dir <path>"

        allowed_prefixes = ["/home/kali/challenges", "/home/kali/secpt", "/tmp"]
        resolved = str(Path(path).resolve())
        if not any(resolved.startswith(p) for p in allowed_prefixes):
            return f"Error: 路径不允许: {path}"

        try:
            p = Path(resolved)
            if not p.is_dir():
                return f"Error: 不是目录: {path}"

            lines: list[str] = []
            for entry in sorted(p.iterdir()):
                try:
                    size = entry.stat().st_size
                    size_str = f"{size:>10d}B" if size < 1024 else f"{size / 1024:>8.1f}KB"
                    marker = "/" if entry.is_dir() else ""
                    lines.append(f"  {entry.name}{marker}  {size_str}")
                except PermissionError:
                    lines.append(f"  {entry.name}  [权限不足]")

            output = f"目录 {resolved} ({len(lines)} 项):\n" + "\n".join(lines)
            if len(output) > self.MAX_OUTPUT:
                output = output[:self.MAX_OUTPUT] + "\n... [truncated]"
            return output
        except FileNotFoundError:
            return f"Error: 目录不存在: {path}"
        except PermissionError:
            return f"Error: 权限不足: {path}"
        except Exception as e:
            return f"Error: {e}"


__all__ = ["ListDirTool", "ReadFileTool"]
