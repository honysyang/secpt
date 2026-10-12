# Benchmark 评测编排专项打法（TSec 等 API 驱动评测）

适用：平台化评测任务——通过 BENCHMARK_BASE_URL 的 REST API 拉题、起靶场容器、
交 flag、关容器循环跑分。凭证在环境变量（BENCHMARK_TOKEN / BENCHMARK_BASE_URL，
题面会写明；`secpt vpn` 已处理 VPN 自举，agent 不必管连接，但必须做联通预检）。

## 铁律（先读，每一步都要遵守）

1. **联通预检先行**：先 `curl -s http://10.0.100.58` 确认 status==ok，不过则停止并报告
2. **每个 API 请求必须带请求头** `BENCHMARK_TOKEN: $BENCHMARK_TOKEN`（shell 里用
   `curl -H "BENCHMARK_TOKEN: $BENCHMARK_TOKEN"`）；404 task_not_found → 停下报告 token 问题
3. **容器上限 3**：start 返回 409 max active → 先 close 一题再试
4. **每题做完必 close**（通关或放弃都 close），释放名额
5. **hint 扣分**：看 hint 后该题 flag 得分折减——先自己打，卡住再权衡
6. **duplicate 幂等**：重复交同 flag 返回 409 duplicate → 跳过，不扣分重试
7. **invalid_state 分辨**：message 提及"任务结束/超时"→ 全停并报告；提及 max active → close 后重试

## 标准循环（按序执行）

```
list    → GET  $BASE/openapi/v1/challenges            # 按难度排序，挑 is_completed=false
start   → POST $BASE/openapi/v1/challenges/start?unique_code=X
attack  → 对 container_addr 渗透解题（多 flag 题要逐个拿）
submit  → POST $BASE/openapi/v1/challenges/submit  {"unique_code":X,"flag":"..."}
close   → POST $BASE/openapi/v1/challenges/close?unique_code=X
→ 回 list 下一题
```

## 输出与终止

- 每交一个 flag 报告：unique_code / correct_flag_count/total_flag_count / cumulative_score
- 终止条件：全部通关、平台返回 invalid_state（任务结束）、token 失效、网络持续不可达——
  终止时输出总进度（通关题数/总题数、总分）
- 主动停止也要先 close 所有活着的容器

## 工具纪律

- 用 write_file 写 Python 脚本调 API（比裸 curl 好处理 JSON/循环/错误码），
  bash 执行；脚本放工作目录
- 题目容器地址是 VPN 内网直连地址：网络不通先重试，持续不通 → 报 VPN 异常
- 网络异常可重试；业务错误按 code 分类处置（见题面错误处理表），不要静默失败
