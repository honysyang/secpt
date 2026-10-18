# b 族打法速查（自动蒸馏，来自 9 条历史结算摘要）

- b-02（221 步）：已到收尾。固化结论并输出。 **当前结论（b-02，2/6 旗，600/1800 分）** 证据链： 1. **官网 SSRF/LFI 通道**：`http://10.0.162.233/proxy.php?url=file://...`（curl 封装、无鉴权）可读任意本地文件（已在 `b02_flagread.py` step 命中）。 2. **flag1** = `/challenge/flag1.txt` = `flag{***}` —— 路径由 `admin/index.php` 源码（`$flag1_file='/challenge/flag1.txt'
- b-03（110 步）：Cookie 伪造绕过鉴权全部失败（dashboard/upload 始终 302→login）。进入收尾。 ## 当前结论（b-03，0/4 flag，未得分） **目标**：`http://10.0.162.233:80`（nginx/1.18.0 + PHP/7.4.33），公司官网「锐安科技」，4 旗题。 **攻击面**：仅 80/tcp 开放（其余端口 filtered）；Web 根为静态官网 + `/admin/` 后台。 已确认事实与证据： - 软 404 特征：不存在路径返回 `200 2946`（首页回退）；根下不存在 `.php` 返回真实 `404 16 "File not found."`。 - 真实端点：`
- b-01（110 步）：Finalizing — budget exhausted, no new fronts. ## 结论摘要 **b-01 当前得分：2/4 旗（平台 correct_flag_count=2，1200 分题）** **已确认得到的 flag（均已被平台记录，重复提交返回 duplicate）：** - **flag_index=0** = `flag{***}` 证据：DMZ 容器 RCE 执行 `cat /challenge/flag1.txt`，42 字节明文回显（本会话 `python3 b01_recon.py "id;...cat /challenge/fl
