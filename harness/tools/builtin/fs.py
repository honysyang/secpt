"""文件系统内置工具：读写文件、列目录、文本搜索。

约定：
- 支持 ~ 与环境变量展开
- 写操作标记 dangerous=True，需人工确认（配合 HITL）
- 读取有大小上限，防止超大文件耗尽上下文
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from harness.tools.decorator import tool

_READ_MAX_BYTES = 100_000
_SEARCH_MAX_RESULTS = 50

# LLM 生成产物（扫描脚本、临时数据）的默认落点：
# 纯文件名的相对路径统一重定向到此目录，避免散落在工作目录根部污染仓库
OUTPUTS_DIR = "outputs"


def resolve_output_path(path: str) -> tuple[Path, bool]:
    """解析写入路径；纯文件名的相对路径重定向到 outputs/ 目录。

    Args:
        path: 原始路径

    Returns:
        (最终路径, 是否被重定向)
    """
    expanded = Path(os.path.expandvars(os.path.expanduser(path)))
    # 纯文件名：parent 为当前目录 —— 无明确目录意图，归入 outputs/
    if not expanded.is_absolute() and expanded.parent == Path("."):
        return Path(OUTPUTS_DIR) / expanded, True
    return expanded, False


@tool
def read_file(path: str, encoding: str = "utf-8", max_bytes: int = _READ_MAX_BYTES) -> str:
    """读取文本文件内容（超长自动截断）。

    Args:
        path: 文件路径
        encoding: 文件编码
        max_bytes: 最大读取字节数
    """
    file_path = Path(os.path.expandvars(os.path.expanduser(path)))
    if not file_path.is_file():
        return f"错误: 文件不存在 {file_path}"

    data = file_path.read_bytes()[:max_bytes]
    text = data.decode(encoding, errors="replace")
    if file_path.stat().st_size > max_bytes:
        text += f"\n... [已截断，共 {file_path.stat().st_size} 字节]"
    return text


@tool(dangerous=True)
def write_file(path: str, content: str) -> str:
    """写入文本文件（覆盖已有内容，自动创建父目录）。

    Args:
        path: 目标文件路径。纯文件名（如 scan.py）会写入 outputs/ 目录；
              指定子目录或绝对路径时按原路径写入
        content: 要写入的内容
    """
    file_path, redirected = resolve_output_path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    message = f"已写入 {len(content.encode('utf-8'))} 字节到 {file_path}"
    if redirected:
        message += "（纯文件名相对路径已重定向到 outputs/）"
    return message


@tool
def list_dir(path: str = ".", show_hidden: bool = False) -> str:
    """列出目录内容，目录名以 / 结尾。

    Args:
        path: 目录路径
        show_hidden: 是否包含隐藏文件
    """
    dir_path = Path(os.path.expandvars(os.path.expanduser(path)))
    if not dir_path.is_dir():
        return f"错误: 目录不存在 {dir_path}"

    entries = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    lines = []
    for entry in entries:
        if not show_hidden and entry.name.startswith("."):
            continue
        suffix = "/" if entry.is_dir() else ""
        lines.append(entry.name + suffix)
    return "\n".join(lines) or "(空目录)"


@tool
def search_text(
    path: str,
    pattern: str,
    max_results: int = _SEARCH_MAX_RESULTS,
    ignore_case: bool = True,
) -> str:
    """在目录下递归搜索匹配正则表达式的文本行。

    输出格式：`文件:行号: 内容`。

    Args:
        path: 起始目录或单个文件
        pattern: 正则表达式
        max_results: 最大返回条数
        ignore_case: 是否忽略大小写
    """
    root = Path(os.path.expandvars(os.path.expanduser(path)))
    if not root.exists():
        return f"错误: 路径不存在 {root}"

    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return f"错误: 无效正则表达式 {exc}"

    files = [root] if root.is_file() else root.rglob("*")
    results: list[str] = []

    for file_path in files:
        if len(results) >= max_results:
            break
        if not file_path.is_file():
            continue
        try:
            with file_path.open(encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, start=1):
                    if regex.search(line):
                        results.append(f"{file_path}:{lineno}: {line.rstrip()}")
                        if len(results) >= max_results:
                            break
        except OSError:
            continue  # 无权限等读取失败，跳过该文件

    return "\n".join(results) or "(无匹配)"
