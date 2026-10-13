# Crypto 专项打法（密码学题）

适用：RSA/AES/PRNG/古典密码/自定义加密协议的 CTF 题。

## 识别与路线（先分类再动手）

### RSA（最高频）
症状：给 n/e/c，有时多组或给部分私钥。攻击清单按代价排序：
1. **小 e / 低熵明文**：e=3 开立方（`gmpy2.iroot`）；多接收者同 e 不同 n → 共模攻击（CRT 聚合后开 e 次方）
2. **小私钥指数**：d < n^0.25 → Wiener（连分数，`sympy` 有 `continued_fraction`）
3. **相似明文广播**： Franklin-Reiter（同 e=3、两明文线性相关 → 结式/sage；无 sage 时用 sympy resultant）
4. **已知部分私钥/高位泄露**：Coppersmith 小根——**有 sage 用 sage，没有就换题或找 hint**，sympy 手写格不划算
5. **Oracle 类**： padding oracle（每 1 bit 泄漏）→ Bleichenbacher 六步，脚本化逐字节推

### AES / 分组模式
- ECB：字节-at-a-time 预言机（逐字节枚举对齐块）
- CBC：padding oracle（篡改末块看合法/非法）或 IV 可控 → 首位翻转任意改明文
- CTR：nonce 复用 → 两密文异或得明文异或，已知其一破其二

### PRNG / 随机数
- `random`（Python/C rand）→ MT19937 状态恢复：624 个 32bit 输出反推（`z3` 或现成脚本）
- 种子可猜（时间戳/pid）→ 直接爆破种子空间

### 古典/编码
- 频率分析 → 维吉尼亚（Kasiski 定长度 + 逐列频率）→ 工具脚本一次跑完

## 工具与纪律

- 主力：`python3 + pycryptodome + sympy + gmpy2 + z3-solver`（沙箱已装）
- **先写数学推导再写代码**：纸上把攻击公式推完，z3/脚本只是执行器——
  无推导的爆破是赌博
- 模运算全程 `gmpy2.mpz` 防溢出；幂用 `pow(a, e, n)`（三参数）
- 每步用小样例验证（e=3、p=17*19 自己造）再上真数据
- 需要 sage 的题（Coppersmith/格基为主）：先尝试找等效 z3 路径，找不到→
  看 hint 或放弃，**不要在无 sage 环境硬刚格基**
