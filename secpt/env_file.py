"""可选 `.env` 装载（M5-20）：只认显式路径，绝不隐式读当前目录。

为什么不做"自动读 ./.env"：`secpt` 是渗透工具，配置里握着 API key 与执行隔离
开关；隐式读 cwd 意味着"在哪个目录运行就吃哪份配置"，把目录变成攻击面。这里
只认 ``SECPT_ENV_FILE`` 显式指路，且真实环境变量永远优先（容器/CI 里注入的
值不会被文件覆盖）。

安全边界（两道）：
1. **键白名单**：只接受 ``SECPT_*`` 与 ``LLM_PROVIDER``——.env 里写 ``PATH``/
   ``HOME`` 之类不会被采纳，防"配错文件改坏进程环境"；
2. **不执行任何 shell 语法**：只支持 ``KEY=VALUE`` / ``export KEY=VALUE`` /
   去引号 / ``#`` 注释，不做变量展开，不 source 脚本。

格式不支持时静默跳过该行（配置文件是数据不是代码，坏行不该炸进程）；
文件本身不存在才是配置错误 → ``E_CONFIG`` 拒跑（显式指路却指空，是笔误）。
"""

from __future__ import annotations

import os
from pathlib import Path

from secpt.errors import SecptError

ENV_FILE_VAR = "SECPT_ENV_FILE"
_ALLOWED_PREFIX = "SECPT_"
_ALLOWED_EXACT = frozenset({"LLM_PROVIDER"})
_QUOTES = ("'", '"')


def _valid_key(key: str) -> bool:
    """环境变量名合法性：首字符字母/下划线，其余字母数字下划线（拒 ``1A``/``A B``）。"""
    if not key or not (key[0].isalpha() or key[0] == "_"):
        return False
    return all(ch.isalnum() or ch == "_" for ch in key)


def parse_env_text(text: str) -> dict[str, str]:
    """解析 .env 文本为键值字典（纯函数，不碰 os.environ）。

    支持：空行/``#`` 注释/``export`` 前缀/单双引号包裹/首尾空白；
    不支持：变量展开、多行值、``${}`` 引用——那些属于 shell，不属于配置文件。
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not _valid_key(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
            value = value[1:-1]
        values[key] = value
    return values


def load_env_file(path: str | Path) -> dict[str, str]:
    """读文件并只保留白名单键。

    Raises:
        SecptError: 文件不存在（``SECPT_ENV_FILE`` 显式指路却指空 = 笔误）。
    """
    file = Path(path)
    if not file.is_file():
        raise SecptError(
            f"{ENV_FILE_VAR} 指向的文件不存在",
            code="E_CONFIG",
            context={"path": str(file)},
        )
    parsed = parse_env_text(file.read_text(encoding="utf-8"))
    return {
        key: value
        for key, value in parsed.items()
        if key.startswith(_ALLOWED_PREFIX) or key in _ALLOWED_EXACT
    }


def apply_env_file(path: str | Path) -> list[str]:
    """把文件里的白名单键写进进程环境（已存在的键不覆盖），返回实际生效的键。"""
    applied: list[str] = []
    for key, value in load_env_file(path).items():
        if os.environ.get(key):
            continue  # 真实环境优先：容器/CI/临时 export 永远赢过文件
        os.environ[key] = value
        applied.append(key)
    return applied


def bootstrap_env() -> list[str]:
    """按 ``SECPT_ENV_FILE`` 装载 .env；未设置则不读任何文件（返回 []）。

    在 ``main()`` 入口调用一次：之后所有 ``os.environ.get`` 都能看到文件里的值，
    装配层无需感知"值从哪来"。
    """
    path = os.environ.get(ENV_FILE_VAR, "").strip()
    if not path:
        return []
    return apply_env_file(path)


__all__ = ["ENV_FILE_VAR", "apply_env_file", "bootstrap_env", "load_env_file", "parse_env_text"]
