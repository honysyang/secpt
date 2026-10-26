---
id: source-review
applies_to: [code]
trigger: 需要读源码/配置/依赖清单，确认漏洞可达性
summary: fs.read 上下文回读 + fs grep 定点扫描，结论必须挂行号与调用链
---
# 源码审计要点

- 附件在 workspace 的 `_attachments/` 下：fs.read 逐文件读，fs.grep 定点扫（pattern + glob 如 `*.py`，ignore_case 兜底大小写）。
- 每个结论必须带：文件名 + 行号 + 入口到 sink 的调用链。只写「文件:行号」不给调用链的，是观察不是发现。
- 引擎 file-scan 提案（findings 台账）是起点不是终点：复核可达性后，用自己的 finish.findings 提交升级/降级/排除的结论。
- grep 连击节奏：一类 sink 一次 grep，命中即回读，不要一次 grep 十个 pattern 把输出撑爆。
