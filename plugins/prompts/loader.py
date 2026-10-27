"""提示词模板管理。"""

from __future__ import annotations

from pathlib import Path


class PromptLibrary:
    """提示词库：从文件加载或内联定义。"""

    def __init__(self, prompts_dir: Path | None = None) -> None:
        self.prompts_dir = prompts_dir or Path(__file__).parent
        self._cache: dict[str, str] = {}

    def load(self, name: str) -> str:
        """加载提示词模板。"""
        if name in self._cache:
            return self._cache[name]

        path = self.prompts_dir / f"{name}.md"
        if path.exists():
            content = path.read_text(encoding="utf-8")
        else:
            content = self._fallback(name)

        self._cache[name] = content
        return content

    def _fallback(self, name: str) -> str:
        """内置回退提示词。"""
        fallbacks = {
            "coordinator": "你是智能助理的总调度，根据用户需求路由到合适的专家。",
            "researcher": "你是研究员，通过工具收集信息并标注来源。",
            "writer": "你是专业写手，根据资料撰写结构化内容。",
            "coder": "你是资深程序员，提供可运行的代码和解释。",
        }
        return fallbacks.get(name, f"你是 {name}，请完成任务。")


# 全局库
library = PromptLibrary()
