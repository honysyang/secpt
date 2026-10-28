"""工具层：外部命令适配器 + 内嵌工具登记（§3.5 进程型工具库）。

写入口纪律：本层不直接写账本——小票一律由主循环经 store/ledger 追加；
本层只署名回执（赋能小票三字段：exit_code/summary/raw_ref）。
"""

from secpt.tools.registry import ToolManifest, ToolOutput, ToolRegistry

__all__ = ["ToolManifest", "ToolOutput", "ToolRegistry"]
