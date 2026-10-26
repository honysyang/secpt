"""环境变量唯一入口（命名规则：前缀一律 SECPT_）。

所有 os.environ 读取集中在本模块，业务代码不直接 getenv——新增环境变量
先在这里登记名字和类型化读取函数，避免散落魔法字符串。

.env 自动载入：仓库根 .env 在 import 本模块时读入（幂等）——secpt
是 CLI 工具，要求用户每次先 source .env 是多余的摩擦。真实环境变量
永远优先，.env 只填补未设置的键；解析器是数十行的 intentionally
minimal 实现，不引第三方依赖（约束：仅 pydantic/httpx/pyyaml）。
"""
from __future__ import annotations

import os
from pathlib import Path

# ── LLM 客户端（pa/infra/llm.py） ──
BASE_URL = "SECPT_BASE_URL"
API_KEY = "SECPT_API_KEY"
MODEL = "SECPT_MODEL"
FALLBACK_MODEL = "SECPT_FALLBACK_MODEL"
LLM_MOCK = "SECPT_LLM_MOCK"          # MockLLM 脚本 JSON（测试/演示用）

# ── 运行期（pa/runtime/loop.py） ──
DIE_AT_STEP = "SECPT_DIE_AT_STEP"    # 测试钩子：第 N 个超级步 os._exit(9)

# ── 教练（pa/policy/coach.py） ──
MENTOR_MODE = "SECPT_MENTOR_MODE"    # off | shadow | enforce，默认 shadow

_MENTOR_MODES = ("off", "shadow", "enforce")


def _load_dotenv(path: Path) -> int:
    """最小 .env 解析：KEY=VALUE 行，# 注释，值剥引号；不覆盖已存在的
    环境变量。返回载入的键数（文件缺失 = 0，调用方无感）。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    n = 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v
            n += 1
    return n


def load_dotenv() -> None:
    """仓库根 .env 自动载入（幂等，真实环境优先）。"""
    global _DOTENV_DONE
    if _DOTENV_DONE:
        return
    _DOTENV_DONE = True
    _load_dotenv(Path(__file__).resolve().parents[1] / ".env")


_DOTENV_DONE = False
load_dotenv()


def get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def llm_mock_script() -> str | None:
    """SECPT_LLM_MOCK：未设置返回 None（走真实客户端）。"""
    return os.environ.get(LLM_MOCK) or None


def die_at_step() -> int | None:
    """SECPT_DIE_AT_STEP：正整数或 None。"""
    raw = os.environ.get(DIE_AT_STEP)
    return int(raw) if raw else None


def mentor_mode() -> str:
    """SECPT_MENTOR_MODE：三档之一，非法值回退 shadow（保守：只观测不动手）。"""
    m = os.environ.get(MENTOR_MODE, "shadow").lower()
    return m if m in _MENTOR_MODES else "shadow"
