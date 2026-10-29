---
id: port-scan
title: 端口与服务识别
description: 先摸开放端口与服务版本，是后续一切动作的地基
keywords:
  - 端口
  - nmap
  - port
  - 服务
---
1. 先粗扫常见端口、再视情况全量扫，避免一上来就全量拖时间；
2. -sV 拿服务版本，banner 原文写进 detail，不加工、不推断；
3. 同一目标换不同扫描参数可作第二路径（R2 双路径的常见素材）；
4. 扫描输出必须落 raw 文件，申报 finding 时 evidence_ref 指向该文件。
