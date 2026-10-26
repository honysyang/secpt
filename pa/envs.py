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

from pa.infra.log import get_logger

logger = get_logger("secpt.envs")

# ── LLM 客户端（pa/infra/llm.py） ──
BASE_URL = "SECPT_BASE_URL"
API_KEY = "SECPT_API_KEY"
MODEL = "SECPT_MODEL"
FALLBACK_MODEL = "SECPT_FALLBACK_MODEL"
LLM_MOCK = "SECPT_LLM_MOCK"  # MockLLM 脚本 JSON（测试/演示用）

# ── 运行期（pa/runtime/loop.py） ──
DIE_AT_STEP = "SECPT_DIE_AT_STEP"  # 测试钩子：第 N 个超级步 os._exit(9)

# ── 工具（pa/tools/probe.py） ──
PROBE_TIMEOUT = "SECPT_PROBE_TIMEOUT"  # probe 单连接超时天花板（秒级，质量项②）

# ── 长期经验库（pa/core/experience.py，E3） ──
EXPERIENCES = "SECPT_EXPERIENCES"  # 跨 mission 经验库 JSONL 路径；未设置 = 工作区级默认

# ── 附件 advisory 库（pa/core/filerules.py，1day 匹配） ──
ADVISORIES = (
    "SECPT_ADVISORIES"  # JSONL：{ecosystem,name,introduced,fixed,id,severity}；未设置 = 无依赖匹配
)

# ── 上下文（pa/core/context.py，E4 预算裁缝） ──
MAX_CTX = "SECPT_MAX_CTX_CHARS"  # user 总长字符预算；未设置 = 内置缺省
DEFAULT_MAX_CTX_CHARS = 120_000  # BUILD_SPEC §7.1 硬约束 2（≈30k token）

# ── MCP 工具池（pa/tools/mcp.py，M5） ──
MCP_JSON = (
    "SECPT_MCP_JSON"  # MCP server 声明（内联 JSON 或文件路径）；未设置 = 无 MCP 工具
)

# ── 教练（pa/policy/coach.py） ──
MENTOR_MODE = "SECPT_MENTOR_MODE"  # off | shadow | enforce，默认 shadow

# ── 日志（pa/infra/log.py） ──
LOG_LEVEL = "SECPT_LOG_LEVEL"  # DEBUG|INFO|WARNING|ERROR，默认 WARNING
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

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


def experiences_path() -> Path | None:
    """SECPT_EXPERIENCES：跨 mission 经验库（E3）的显式路径；未设置返回
    None——默认「工作区级（mission 目录父级）」需要 mission root 做锚点，
    本层拿不到，由 pa.core.experience.library_path 决定最终路径。"""
    raw = os.environ.get(EXPERIENCES)
    return Path(raw).expanduser() if raw else None


def probe_timeout() -> float:
    """SECPT_PROBE_TIMEOUT：>0 的浮点，默认 5.0。

    probe 的 connect_ex 是秒级动作；gateway 的泛工具默认 120s 是给
    nmap/http 这类重活用的——closed/filtered 端口按 120s 等会在每个
    ReAct 步上白等两分钟，正是 thrash 的来源（WORKFLOW 质量项②）。
    probe 单连接取「调用方预算与 probe 天花板」的较小者。"""
    raw = os.environ.get(PROBE_TIMEOUT)
    if not raw:
        return 5.0
    try:
        v = float(raw)
    except ValueError:
        return 5.0
    return v if v > 0 else 5.0


def mcp_json() -> str | None:
    """SECPT_MCP_JSON：MCP server 声明（M5），内联 JSON 文本或文件路径。

    判定规则：首字符是 { 或 [ 的当内联 JSON；否则当文件路径读文本。
    未设置 / 路径指向不存在的文件 → 返回 None（配置缺席 = 能力缺席，
    MCP 工具池静默不注册——数据驱动配置的保守回退，和「LLM 失败静默
    降级」同一哲学：缺能力不阻断渗透流程）。"""
    raw = os.environ.get(MCP_JSON)
    if not raw:
        return None
    if raw.lstrip()[0] in "{[":
        return raw
    try:
        text = Path(raw).expanduser().read_text(encoding="utf-8")
    except OSError as e:
        # 声明文件读不到 = MCP 能力整体缺席：这是用户显式配置的失约，
        # 必须留痕（配置缺席听不见 = 用户对着空气排查）。
        logger.warning("SECPT_MCP_JSON 指向的文件读不到，MCP 工具池不注册: %s", e)
        return None
    return text or None


def log_level() -> str:
    """SECPT_LOG_LEVEL：DEBUG/INFO/WARNING/ERROR 四档封闭，非法回 WARNING。

    日志是观测不是业务：配错走保守值（WARNING）比炸出去更对，也避免
    「日志配置手误搞挂一次渗透任务」这种荒谬故障。"""
    v = os.environ.get(LOG_LEVEL, "").upper()
    return v if v in _LOG_LEVELS else "WARNING"


def max_ctx_chars() -> int:
    """SECPT_MAX_CTX_CHARS：user 总长字符预算（E4 预算裁缝），内置缺省
    BUILD_SPEC §7.1 硬约束 2 的 120_000（≈30k token）。非法/非正值回
    缺省——预算是护栏不是数据，配错走保守值比炸出去更对。"""
    raw = os.environ.get(MAX_CTX)
    if not raw:
        return DEFAULT_MAX_CTX_CHARS
    try:
        v = int(raw)
    except ValueError:
        return DEFAULT_MAX_CTX_CHARS
    return v if v > 0 else DEFAULT_MAX_CTX_CHARS
