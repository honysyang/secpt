""".env 文件加载器：项目根 .env 中的 KEY=VALUE 注入 os.environ（真实环境变量优先）。

覆盖两类变量：
- ``SECPT_*``        —— pydantic-settings 的 Settings 自动拾取
- ``DEEPSEEK_API_KEY`` / ``SECPT_MODEL_*`` 等非 Settings 字段的直连变量
  （runtime/llm/client.resolve_model、sandbox、intercept 等都直读 os.environ）

规则：
- 文件不存在 → 静默跳过（.env 是可选的，export 永远有效）
- 已存在的环境变量优先（真实 shell 导出 > .env 文件，便于临时覆盖）
- 支持 ``#`` 注释、空行、可选引号；不解析转义（保持 stupidly simple）
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


def load_env_file(path: Path | None = None, *, override: bool = False) -> list[str]:
    """加载 .env；返回实际注入的 key 列表。已加载过的 key 不重复注入。"""
    p = path or project_env_file()
    if not p.is_file():
        return []
    injected: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or key in _LOADED:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
            _LOADED.add(key)
            injected.append(key)
    return injected


# 包被 import 时即加载（任何 secpt.* 模块触发）
load_env_file()
