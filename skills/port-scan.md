---
id: port-scan
applies_to: [recon]
trigger: 需要对目标做端口层面的探活或服务识别
summary: 先 probe 单端口确认，再 nmap -sV 取版本，结果按 host:port 记线索
---
# 端口扫描要点

- 先用 probe 对单端口确认 open/closed，再对开放段跑 `nmap -sV` 取服务与版本。
- 扫描结果以端口为单位记线索：key 用 `port`，value 带 `host:port=state`。
- 全端口扫描前先缩小候选集：从目标清单与已确认服务推断，不盲扫 65535。
