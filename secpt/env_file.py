"""可选 `.env` 装载（M5-21）：显式路径优先，其次自动读当前目录的 `.env`。

装载顺序：
1. ``SECPT_ENV_FILE`` 显式指路（指空文件 → ``E_CONFIG`` 拒跑，不静默跳过）；
2. 未显式指路时自动读 ``./.env``（存在才读）；
3. ``SECPT_DOTENV=0`` 可关闭第 2 步（CI / 单测用，避免仓库根 .env 影响用例）。

安全边界（两道）：
1. **键白名单**：只接受 ``SECPT_*`` 与 ``LLM_PROVIDER``——.env 里写 ``PATH``/
   ``HOME`` 之类不会被采纳，防"配错文件改坏进程环境"；
2. **真实环境变量优先**：``apply_env_file`` 逐键判存在，容器/CI/临时 export
   注入的值永远赢过文件；
3. **不执行任何 shell 语法**：只支持 ``KEY=VALUE`` / ``export KEY=VALUE`` /
   去引号 / ``#`` 注释，不做变量展开，不 source 脚本。

文件里格式不对的行静默跳过（配置是数据不是代码）；显式指路却指空才是配置
错误 → ``E_CONFIG`` 拒跑。
"""

from __future__ import annotations

import os
from pathlib import Path

from secpt.errors import SecptError

ENV_FILE_VAR = "SECPT_ENV_FILE"
DOTENV_TOGGLE = "SECPT_DOTENV"
_DEFAULT_DOTENV = Path(".env")
_OFF_VALUES = frozenset({"0", "false", "no", "off"})
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
    """在 ``main()`` 入口调用一次：按"显式 > 当前目录 .env"装载，返回生效键。

    未设置 ``SECPT_ENV_FILE`` 且当前目录没有 ``.env``、或 ``SECPT_DOTENV=0``
    时什么都不读（返回 []）。
    """
    explicit = os.environ.get(ENV_FILE_VAR, "").strip()
    if explicit:
        return apply_env_file(explicit)
    if os.environ.get(DOTENV_TOGGLE, "").strip().lower() in _OFF_VALUES:
        return []
    if not _DEFAULT_DOTENV.is_file():
        return []
    return apply_env_file(_DEFAULT_DOTENV)


__all__ = [
    "DOTENV_TOGGLE",
    "ENV_FILE_VAR",
    "apply_env_file",
    "bootstrap_env",
    "load_env_file",
    "parse_env_text",
]
