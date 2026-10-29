"""provider 包：LLM 接口层的可插拔后端（开发文档 §6.1：provider 一换全换）。

分层红线：providers 只可被 llms/main.py 导入；每个 provider 只实现
:class:`~secpt.llms.client.LlmProvider` 最小原语 ``complete``——schema 解析、
带错重试、超时指数退避等纪律统一在协议层（LlmClient），不在此处重复。
"""

from secpt.llms.providers.openai_compat import OpenAICompatProvider

__all__ = ["OpenAICompatProvider"]
