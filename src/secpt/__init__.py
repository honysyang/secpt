"""secpt — security penetration testing agent."""

__version__ = "0.1.0"

# 包级副作用：加载项目根 .env（真实环境变量优先；见 config/envfile.py）
from secpt.config.envfile import load_env_file as _load_env_file

_load_env_file()
del _load_env_file
