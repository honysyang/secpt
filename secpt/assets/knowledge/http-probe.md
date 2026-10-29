---
id: http-probe
title: HTTP 面初筛
description: 对已发现的 HTTP 服务做只读探测，确认入口与技术指纹
keywords:
  - http
  - web
  - 网页
  - 站点
---
1. 先取根路径响应头（Server / X-Powered-By）与首页标题，确定技术栈；
2. 只做 GET/HEAD，不做写操作；可疑路径小步试探，不并发轰炸；
3. 记录请求与响应特征（状态码/长度/差异），这是后续验证的锚点；
4. 注入类线索优先用只读回显验证；写操作必须走 Guard/HITL，不可绕过。
