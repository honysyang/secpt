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
