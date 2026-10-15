# Pwn 专项打法（二进制利用）

适用：ELF 二进制题（栈/堆/格式化字符串）、远程 nc/菜单式服务、内核题入门。

## 标准工作流（按序，不要跳步）

1. **摸底**：`checksec <bin>` 定防御面——Canary/NX/PIE/RELRO/Fortify 直接决定攻击路线
2. **交互建模**：`write_file` 写 pwntools 脚本（`from pwn import *`；本地 `process()` 调通再切
   `remote(host, port)`），先跑通菜单/协议，再谈利用——**不要裸 nc 手打**
3. **找漏洞**：源码（附件）> 反汇编（objdump -d / gdb disas）> 黑盒 fuzz。
   高频点：gets/strcpy 栈溢出、printf(user_input) 格式化字符串、
   UAF/double-free（菜单题 free 后_use-after-check）、整数溢出转栈溢出
4. **栈题路线**：cyclic 100 定偏移（`cyclic_find`）→ 看防御选路：
   - 无 Canary 有 win 函数 → ret2win
   - NX 开 → ROP：`ROPgadget --binary bin`；泄露用 puts(puts@got) 打 libc
   - Canary 泄露 → 格式化字符串 `%p` 扫栈 / 栈上残留的 canary 复用
   - PIE 开 → 先泄露基址（栈上返回地址/格式化字符串），再按偏移 ROP
5. **堆题路线**：先定 glibc 版本（`ldd --version` 或附件 libc.so）→
   菜单题先攒泄漏（show 功能的 UAF/格式化）→ tcache poisoning 打 __free_hook
   / __malloc_hook（glibc<2.34）或 house of xxx 系列；**double check 分配尺寸与 tcache bin**
6. **格式化字符串**：`FmtStr` 自动化（pwntools `FmtStr(execute_fmt=...)` 定偏移）→
   任意写改 GOT/栈变量；%n 家族按地址宽度选（hn/hhn 分两次写）
7. **验证闭环**：本地打通 → 远程复现 → 拿到 flag 再动手写报告

## 纪律

- **一切交互脚本化**：pwntools 脚本是唯一事实来源，调试迭代改脚本不手敲
- 脚本名带阶段后缀（exploit_leak.py / exploit_final.py），别覆盖能跑的版本
- 泄露型漏洞先稳 leak（跑 5 次全成）再搭利用链，否则 PIE/ASLR 会教你做人
- glibc 版本不匹配是远程打不通的首因——附件给 libc.so 就用它（pwn 脚本地加载）

## 环境与执行经验（实战教训）

- **bash 工具超时默认 60s**：QEMU 启动 ~21-25s（负载高时 233s）+ 交互极易超时被斩。
  跑远程长流程必须**后台分离 + 日志轮询**：write_file 写启动器（`subprocess.Popen(..., start_new_session=True)`，
  stdout 重定向到 log），再 `python3 -c "import time;time.sleep(50)"` 等待后 `cat log` 轮询；
  不要用超时的前台单条命令。
- **等待标记会匹配到回显的命令行本身**（`echo E_ID` 会被串口回显，`readuntil(E_ID)` 提前返回）：
  waitfor/recvuntil 用**唯一、且不出现在命令文本里的标记**，或按正则累积读到完整行（4 个 leak 值一次拿全）。
- **多行 for/if 逐行发送会被 PS2 续行提示打断**：串口交互要逐行同步（每条命令等到提示符再发下一条），
  避免一次性 send 整段 shell 片段导致丢字节/行序错乱。
- **容器工具缺失是常态**：curl/unzip/zipinfo 可能没装、`which`/`cd` 不在白名单——
  直接用 `python3 -c` / zipfile 模块代偿，别反复重试同一条命令浪费步数。
- **远程题单实例竞争**：上一连接未释放会 SSL handshake 失败——先 sleep 退避重试；
  清理挂起客户端用 /proc 读 cmdline 手杀（killrecon 模式），别依赖 ps。
- **boot 时长漂移（21s→233s）**：题目有 300s 时限时，boot 慢会耗尽配额——
  先做带超时的连通性探测（socket+TLS+读内核日志判引导进度），再决定是否完整跑 exploit；
  高负载期减少多轮探测，改为单次干净运行。
