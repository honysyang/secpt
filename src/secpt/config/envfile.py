""".env 文件加载器：项目根 .env 中的 KEY=VALUE 注入 os.environ（真实环境变量优先）。

覆盖两类变量：
- ``SECPT_*``        —— pydantic-settings 的 Settings 自动拾取
- ``DEEPSEEK_API_KEY`` / ``SECPT_MODEL_*`` 等非 Settings 字段的直连变量
  （runtime/llm/client.resolve_model、sandbox、intercept 等都直读 os.environ）

规则：
- 文件不存在 → 静默跳过（.env 是可选的，export 永远有效）
- 已存在的环境变量优先（真实 shell 导出 > .env 文件，便于临时覆盖）
- 支持 ``#`` 注释、空行、``export KEY=VALUE`` 前缀、成对引号、行内注释
  （``KEY=value # 注释``；``#`` 前须有空白，URL fragment 等无空白场景不受影响）
- 不解析转义/变量插值（保持 stupidly simple）
- 路径：项目根 .env，或 ``SECPT_ENV_FILE`` 指定
"""

from __future__ import annotations

import os
from pathlib import Path

_LOADED: set[str] = set()


def project_env_file() -> Path:
    override = os.environ.get("SECPT_ENV_FILE")
    if override:
        return Path(override)
    # src/secpt/config/envfile.py → 项目根（parents: config→secpt→src→root）
    return Path(__file__).resolve().parents[3] / ".env"


def _clean_value(raw: str) -> str:
    """剥引号与行内注释：成对引号整体剥（引号内 # 保留）；未加引号时空白+``#`` 起注释。"""
    if raw[:1] in ('"', "'"):
        end = raw.find(raw[0], 1)
        if end != -1:
            return raw[1:end]
        # 引号不闭合：按无引号值继续处理
    for i, ch in enumerate(raw):
        if ch == "#" and i > 0 and raw[i - 1] in (" ", "\t"):
            return raw[:i].rstrip()
    return raw


def _parse_line(line: str) -> tuple[str, str] | None:
    """解析单行 KEY=VALUE；注释/空行/无 ``=`` 行返回 None。"""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export ") or line.startswith("export\t"):
        line = line[7:].lstrip()
    key, _, value = line.partition("=")
    key = key.strip()
    if not key:
        return None
    return key, _clean_value(value.strip())


def parse_env_file(path: Path) -> dict[str, str]:
    """解析 .env 为 KEY→VALUE（不注入环境变量；doctor/调试等只读场景用）。"""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_line(line)
        if parsed:
            out[parsed[0]] = parsed[1]
    return out


def load_env_file(path: Path | None = None, *, override: bool = False) -> list[str]:
    """加载 .env；返回实际注入的 key 列表。已加载过的 key 不重复注入。"""
    injected: list[str] = []
    for key, value in parse_env_file(path or project_env_file()).items():
        if key in _LOADED:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
            _LOADED.add(key)
            injected.append(key)
    return injected


# 包被 import 时即加载（任何 secpt.* 模块触发）
load_env_file()
