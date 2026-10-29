---
id: web-surface
name: web 面初筛
description: 对已发现的 HTTP 服务做响应头与路径初筛（只读）
prompts:
  - skills.evidence
---
1. 先取根路径的响应头与首页标题，确认服务类型与版本；
2. 只做只读探测（GET/HEAD），不做写操作、不触发业务副作用；
3. 发现可疑入口点后申报，detail 写清请求特征与响应特征。
