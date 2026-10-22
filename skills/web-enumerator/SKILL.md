---
name: web-enumerator
description: Web 入口枚举能力包：路由/参数/认证面系统化清点
when_to_use: 目标是 HTTP 服务且任务含"枚举入口/摸清攻击面/路由发现"；先于利用执行
---
# Web 入口枚举

1. 先用 whatweb/httpx 定技术栈与虚拟路由线索（VHost/CDN/WAF）。
2. gobuster 双字典（目录 + 扩展名）打底，nuclei  exposure 模板补漏。
3. 每个发现的端点至少一次真实交互（http_request），记录：方法、参数、认证要求、响应差异。
4. 产出《入口清单》：端点 → 参数 → 认证面 → 可疑行为，交给利用任务选点。
