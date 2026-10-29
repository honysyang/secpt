"""pytest 全局护栏。

M5-21 起 ``secpt`` 会自动加载当前目录的 ``.env``；仓库根就有一份（含真 key 与
``LLM_PROVIDER=openai``），若不关掉，会污染所有"未显式设置 provider"的用例。
这里统一关闭 cwd 自动加载（显式 ``SECPT_ENV_FILE`` 的用例不受影响）。
"""

import os

os.environ.setdefault("SECPT_DOTENV", "0")
