"""CWE 映射：LLM 分类 + 内置字典校验（防幻觉编号）。

黑客逻辑：报告的价值一半在分类法——"SAML 签名包装"不如 "CWE-347: Improper
Verification of Cryptographic Signature" 可被客户/社区检索、比对、统计。
LLM 负责从候选中挑，字典负责"编号必须真实存在"这道硬校验。
"""

from __future__ import annotations

import json
import re

# 内置常用 CWE 字典（Web/渗透高频；后续按需扩充——字典是唯一事实源）
CWE_DICT: dict[str, str] = {
    "CWE-79": "在Web页面生成时对非受信数据的不当转义（XSS）",
    "CWE-89": "SQL命令中特殊元素未中和（SQL注入）",
    "CWE-22": "对路径名的限制不当（路径穿越）",
    "CWE-23": "相对路径遍历",
    "CWE-94": "代码生成控制不当（代码注入）",
    "CWE-95": "Eval注入",
    "CWE-77": "命令注入",
    "CWE-78": "OS命令中特殊元素未中和（OS命令注入）",
    "CWE-502": "不可信数据反序列化",
    "CWE-611": "XML外部实体引用限制不当（XXE）",
    "CWE-918": "服务器端请求伪造（SSRF）",
    "CWE-434": "对危险类型文件的上传限制不当",
    "CWE-287": "认证机制不当",
    "CWE-288": "使用替代路径或通道绕过认证",
    "CWE-290": "伪装攻击（Spoofing）",
    "CWE-294": "捕获重放攻击",
    "CWE-305": "关键功能认证缺失",
    "CWE-306": "关键功能缺少认证",
    "CWE-307": "认证尝试次数限制不当（可暴力破解）",
    "CWE-308": "单因素认证的使用",
    "CWE-384": "会话固定",
    "CWE-521": "弱口令要求",
    "CWE-613": "会话过期时间不足",
    "CWE-620": "未验证的口令修改",
    "CWE-640": "口令恢复机制弱点",
    "CWE-798": "硬编码凭据",
    "CWE-863": "授权机制不正确",
    "CWE-862": "缺少授权",
    "CWE-269": "特权管理不当",
    "CWE-250": "不必要的特权执行",
    "CWE-268": "特权链不当",
    "CWE-284": "访问控制不当",
    "CWE-285": "授权决策不当",
    "CWE-639": "通过用户控制密钥的授权绕过（IDOR）",
    "CWE-200": "敏感信息暴露",
    "CWE-201": "通过发送数据暴露敏感信息",
    "CWE-203": "可观察差异暴露信息（响应时间/错误差异）",
    "CWE-209": "错误消息暴露敏感信息",
    "CWE-215": "调试信息暴露",
    "CWE-548": "目录列表暴露",
    "CWE-312": "敏感信息明文存储",
    "CWE-319": "敏感信息明文传输",
    "CWE-321": "硬编码加密密钥",
    "CWE-327": "使用已被攻破或存在风险的加密算法",
    "CWE-330": "使用不充分的随机值",
    "CWE-347": "密码签名验证不当（含XML签名包装/XSW）",
    "CWE-345": "数据真实性验证不足",
    "CWE-352": "跨站请求伪造（CSRF）",
    "CWE-358": "不恰当执行同一动作等幂性（重放）",
    "CWE-472": "对外部控制指针/索引引用不当（参数污染）",
    "CWE-601": "重定向验证不当（开放重定向）",
    "CWE-610": "资源在另一资源的外部控制引用（XSPA）",
    "CWE-614": "Secure属性缺失的敏感Cookie",
    "CWE-1004": "HttpOnly属性缺失的敏感Cookie",
    "CWE-942": "Cookie权限控制不当（越权参数）",
    "CWE-16": "配置不当",
    "CWE-706": "变量名/方法名解析不当",
    "CWE-843": "类型混淆",
    "CWE-696": "行为顺序不正确（TOCTOU类）",
    "CWE-362": "竞争条件",
    "CWE-400": "未受控资源消耗（DoS）",
    "CWE-770": "未受控的资源分配",
    "CWE-1021": "UI中的不当帧显示（点击劫持）",
    "CWE-693": "保护机制失效",
    "CWE-223": "安全相关决策信息不足",
    "CWE-732": "权限分配不当",
    "CWE-915": "表达式语言语句外部控制（EL注入）",
    "CWE-917": "表达式语言语句外部控制（OGNL/SSTI）",
}

# 关键词预匹配（CWE → 触发词），先缩小候选再让 LLM 终裁，省钱且防幻觉
_CWE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "CWE-79": ("xss", "跨站脚本", "script>", "反射型", "存储型"),
    "CWE-89": ("sql", "注入", "injection", "union select", "sqli"),
    "CWE-22": ("路径穿越", "目录遍历", "../", "path traversal"),
    "CWE-78": ("命令注入", "command injection", "os 命令", "rce"),
    "CWE-94": ("代码注入", "code injection", "模板注入", "ssti"),
    "CWE-917": ("ssti", "模板注入", "freemarker", "thymeleaf", "ognl"),
    "CWE-502": ("反序列化", "deserialization", "pickle", "serialize"),
    "CWE-611": ("xxe", "xml 外部实体", "xml external"),
    "CWE-918": ("ssrf", "服务端请求伪造", "url 参数访问内网"),
    "CWE-434": ("文件上传", "upload", "任意文件"),
    "CWE-306": ("未认证", "无需登录", "missing authentication", "未授权访问"),
    "CWE-862": ("缺少授权", "越权", "未授权访问", "idor", "水平越权", "垂直越权"),
    "CWE-639": ("idor", "水平越权", "直接对象引用"),
    "CWE-863": ("垂直越权", "权限提升", "提权", "privilege"),
    "CWE-287": ("认证绕过", "认证缺陷", "auth bypass"),
    "CWE-384": ("会话固定", "session fixation"),
    "CWE-798": ("硬编码", "硬编码口令", "硬编码密钥", "hardcoded"),
    "CWE-321": ("硬编码密钥", "hardcoded key"),
    "CWE-319": ("明文传输", "http 明文", "未加密传输"),
    "CWE-327": ("弱加密", "md5 口令", "des 加密", "弱算法"),
    "CWE-330": ("弱随机", "可预测令牌", "伪随机", "prng"),
    "CWE-347": ("签名", "saml", "xsw", "signature wrapping", "签名校验"),
    "CWE-345": ("真实性", "未校验来源"),
    "CWE-352": ("csrf", "跨站请求伪造"),
    "CWE-601": ("开放重定向", "redirect", "url 跳转"),
    "CWE-200": ("信息泄露", "敏感信息", "信息暴露"),
    "CWE-209": ("错误信息", "堆栈泄露", "报错信息", "debug"),
    "CWE-548": ("目录遍历", "目录列表", "dir listing"),
    "CWE-1004": ("httponly", "cookie"),
    "CWE-614": ("secure 属性", "cookie"),
    "CWE-942": ("cookie 权限", "cookie 越权"),
    "CWE-1021": ("点击劫持", "clickjacking", "x-frame"),
}

_CWE_RE = re.compile(r"CWE-\d+")


def candidates(text: str, limit: int = 12) -> list[tuple[str, int]]:
    """关键词预打分 → [(cwe_id, score)]，供 LLM 终裁的候选集。"""
    low = text.lower()
    scored: list[tuple[str, int]] = []
    for cwe, kws in _CWE_KEYWORDS.items():
        base = cwe.split("-1")[0]
        if base not in CWE_DICT:
            continue
        s = sum(1 for k in kws if k in low)
        if s:
            scored.append((base, s))
    scored.sort(key=lambda x: -x[1])
    return scored[:limit]


async def classify(
    title: str, description: str, llm=None, model: str = "deepseek-chat"
) -> list[dict]:
    """LLM 从候选中挑 CWE（最多 3 个），编号必须过字典校验。

    无 llm / 解析失败 → 退回纯关键词打分。返回 [{cwe, name, rationale}]。
    """
    text = f"{title}\n{description}"
    cands = candidates(text)
    if not cands:
        return []
    cand_lines = "\n".join(f"  {cid}: {CWE_DICT[cid]}" for cid, _ in cands)
    if llm is None:
        return [{"cwe": cid, "name": CWE_DICT[cid], "rationale": "keyword"} for cid, _ in cands[:3]]

    prompt = f"""你是 CWE 分类器。根据漏洞描述，从下面的候选 CWE 中选最多 3 个最贴切的，
给出编号与一句话理由。只允许使用候选列表里的编号。

【漏洞】
{text[:1500]}

【候选】
{cand_lines}

只输出 JSON：[{{"cwe": "CWE-89", "rationale": "..."}}]"""
    try:
        if hasattr(llm, "call"):
            resp = await llm.call([{"role": "user", "content": prompt}], [], model)
        else:
            resp = await llm([{"role": "user", "content": prompt}], [], model, "")
        content = (resp.choices[0].message.content or "").strip()
    except Exception:
        return [{"cwe": cid, "name": CWE_DICT[cid], "rationale": "keyword"} for cid, _ in cands[:3]]
    start, end = content.find("["), content.rfind("]")
    if start < 0 or end <= start:
        return [{"cwe": cid, "name": CWE_DICT[cid], "rationale": "keyword"} for cid, _ in cands[:3]]
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return [{"cwe": cid, "name": CWE_DICT[cid], "rationale": "keyword"} for cid, _ in cands[:3]]
    out = []
    for item in data[:3]:
        cwe = str(item.get("cwe", ""))
        m = _CWE_RE.search(cwe)
        if not m:
            continue
        cwe = m.group(0)
        if cwe in CWE_DICT:  # 硬校验：幻觉编号直接丢弃
            out.append(
                {
                    "cwe": cwe,
                    "name": CWE_DICT[cwe],
                    "rationale": str(item.get("rationale", ""))[:200],
                }
            )
    if not out:
        # LLM 输出全部被校验丢弃（幻觉）→ 回退关键词，分类永远有兜底
        return [
            {"cwe": cid, "name": CWE_DICT[cid], "rationale": "keyword(fallback)"}
            for cid, _ in cands[:3]
        ]
    return out


def format_cwe(cwes: list[dict]) -> str:
    """report 用的一行格式化。"""
    return "；".join(f"{c['cwe']} {c['name']}" for c in cwes)
