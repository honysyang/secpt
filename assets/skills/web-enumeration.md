---
id: web-enumeration
applies_to: [web]
trigger: 目标暴露 HTTP 服务，需要枚举路径与响应
summary: 先 GET 根路径确认可达，再枚举常见路径，3xx 记 Location 不跟随
---
# Web 枚举要点

- 先用 http 工具 GET 根路径确认可达性与状态码，再枚举常见路径（robots.txt、sitemap.xml、.well-known）。
- 每个响应记线索：key 用 `path`，value 带 `status:bytes`；跳转不跟随，把 3xx 的 Location 记为线索。
- 看到登录表单或管理路径时，先记线索报给 core，不直接尝试凭据。
