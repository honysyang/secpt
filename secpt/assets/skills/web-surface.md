---
id: web-surface
name: web 面初筛
description: 对已发现的 HTTP 服务做响应头与路径初筛（只读）
tools:
  - http_get
prompts:
  - skills.evidence
---
1. 用 http_get 只读 GET 根路径，响应落成 raw 证据（response 头与首页正文都在里面）；
2. 只做只读探测（GET/HEAD），不做写操作、不触发业务副作用；
3. 发现可疑入口点后申报，evidence_ref 指向 http_get 落下的 raw 文件。
