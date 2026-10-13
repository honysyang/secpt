"""S4 两级路由：规则关键词优先，规则未命中 → deepseek-chat 一次 5 分类。

理由：路由错了代价极高（可能白跑 20 小时），规则确定性优先于 LLM 灵活性。
路由结果必须落 events（type=routed），便于事后核查。
"""

from __future__ import annotations

import contextlib

# 优先级即列表顺序：ctf > redteam > codeaudit > assessment；都未命中 → pentest
RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        "pwn",
        (
            "pwn",
            "二进制",
            "栈溢出",
            "堆",
            "格式化字符串",
            "elf",
            "rop",
            "canary",
            "ret2",
            "uaf",
            "double free",
            "tcache",
            "pwntools",
            "ncat",
            "remote(",
            "escape",
            "libc",
            "reverse",
            "逆向",
        ),
    ),
    (
        "crypto",
        (
            "crypto",
            "密码",
            "加密",
            "rsa",
            "aes",
            "密文",
            "明文",
            "公钥",
            "私钥",
            "padding oracle",
            "prng",
            "随机数",
            "维吉尼亚",
            "base64 解码",
            "取证",
        ),
    ),
    (
        "benchmark",
        (
            "benchmark",
            "tsec",
            "跑分",
            "评测任务",
            "openapi/v1/challenges",
            "unique_code",
            "container_addr",
        ),
    ),
    (
        "ctf",
        (
            "flag",
            "ctf",
            "rc{",
            "flag{",
            "ctf{",
            "解题",
            "题目",
            "挑战",
            "writeup",
            "解题思路",
            "夺旗",
            "pwn",
            "misc",
            "crypto",
        ),
    ),
    (
        "redteam",
        (
            "红队",
            "攻击链",
            "red team",
            "redteam",
            "横向移动",
            "权限维持",
            "apt",
            "对抗演练",
            "攻防演练",
            "钓鱼",
        ),
    ),
    (
        "codeaudit",
        (
            "审计",
            "源码",
            "代码",
            "code audit",
            "codeaudit",
            "源代码",
            "静态",
            "sink",
            "危险函数",
            "漏洞分析",
            "代码审计",
            "审查代码",
        ),
    ),
    (
        "assessment",
        (
            "合规",
            "评估",
            "基线",
            "检查清单",
            "等保",
            "assessment",
            "compliance",
            "安全评估",
            "风险清单",
            "加固",
        ),
    ),
]

DEFAULT_PROFILE = "pentest"

_CLASSIFY_PROMPT = (
    "把下面的任务文本归类到一类，只输出一个词：ctf / pentest / redteam / "
    "assessment / codeaudit / pwn / crypto / benchmark / hunter。不要解释。\n"
    "各类特征：pwn=二进制利用(溢出/ROP/ncat/ELF)；crypto=密码学(RSA/AES/密文)；"
    "benchmark=评测跑分(API 拉题交 flag)；hunter=0day 挖洞找新漏洞；"
    "ctf=夺旗解题；pentest=授权渗透。\n任务文本：\n{text}"
)


def _match_rules(text: str) -> list[str]:
    t = text.lower()
    return [name for name, kws in RULES if any(k in t for k in kws)]


async def _llm_classify(objective: str, llm, api_key: str) -> str:
    """deepseek-chat 一次分类，temperature=0；失败回退默认。"""
    try:
        from secpt.runtime.llm.client import call_llm

        resp = await call_llm(
            [{"role": "user", "content": _CLASSIFY_PROMPT.format(text=objective[:2000])}],
            [],
            "deepseek-chat",
            api_key,
        )
        ans = (resp.choices[0].message.content or "").strip().lower()
        for name, _ in RULES:
            if name in ans:
                return name
        # 规则外的合法 profile 名也接受（LLM 兜底可达 pwn/crypto/benchmark/hunter）
        for extra in ("pwn", "crypto", "benchmark", "hunter"):
            if extra in ans:
                return extra
        return DEFAULT_PROFILE
    except Exception:
        return DEFAULT_PROFILE


async def route_objective(
    objective: str,
    *,
    llm=None,
    api_key: str = "",
    sink=None,
    job_id: str = "",
    round_: int = 0,
) -> str:
    """返回 profile 名。默认 **AI 语义优先**：LLM 理解题意选打法，关键词规则
    只在 LLM 不可用（无 key/网络故障）时兜底——关键词是脆弱的（bbox 复盘：
    题面没写"栈溢出"但语义是 pwn，规则曾漏判）。

    确定性优先模式：SECPT_ROUTE_RULES_FIRST=1（无 LLM 的 CI/离线环境）。
    结果落 routed 事件（含 via：llm / rule / rule(fallback) / default）。
    """
    matched = _match_rules(objective)
    rules_first = os.environ.get("SECPT_ROUTE_RULES_FIRST") == "1"
    llm_ready = llm is not None or bool(api_key) or bool(os.environ.get("DEEPSEEK_API_KEY"))

    if rules_first or not llm_ready:
        if matched:
            profile, via = matched[0], "rule" + ("(fallback)" if not rules_first else "")
        else:
            profile, via = DEFAULT_PROFILE, "default"
    else:
        try:
            profile = await _llm_classify(objective, llm, api_key)
            via = "llm"
        except Exception:
            if matched:
                profile, via = matched[0], "rule(fallback)"
            else:
                profile, via = DEFAULT_PROFILE, "default"

    if sink is not None:
        with contextlib.suppress(Exception):
            sink.write(
                "routed",
                {
                    "profile": profile,
                    "via": via,
                    "matched": matched,
                    "objective_head": objective[:120],
                },
                round_=round_,
            )
    return profile
