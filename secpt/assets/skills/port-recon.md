---
id: port-recon
name: 端口摸底
description: 对目标做端口与服务识别，产出可申报的暴露面证据
prompts:
  - skills.evidence
---
1. 先对目标常见端口（22,80,443 等）做 nmap 摸扫，拿 banner 与服务版本；
2. 有开放端口就落 raw 产物，detail 按 banner 原文写，不加工、不推断；
3. 若目标像 HTTP 面，转 web-surface 技能继续深挖。
