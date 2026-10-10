"""已知 CVE 去重：防止把 1day 当 0day 报（信誉保护）。

黑客逻辑：挖洞的交付价值在"新"。报一个 2021 年就有 CVE 的洞当 0day，
一次就消耗掉客户/社区对你的信任。所以 confirmed 前必须过一道：
"这是已知漏洞的复现吗？"

实现：LLM 判断 + 编号硬校验（CVE-\d{4}-\d{4,} 格式 + 年份合理）。
不联网比对 NVD（内网可用性优先）——LLM 的训练知识足够覆盖知名 CVE，
且结论只是"候选"，报告里标注待人工确认。
"""

from __future__ import annotations

import json
import re

_CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}")


def _valid_cve(cve: str) -> bool:
    m = _CVE_RE.fullmatch(cve)
    if not m:
        return False
    year = int(cve.split("-")[1])
    return 1999 <= year <= 2100


async def dedup_known(
    title: str, description: str, vuln_class: str = "", llm=None, model: str = "deepseek-chat"
) -> list[str]:
    """返回该发现可能复现的已知 CVE 候选（空列表 = 未发现已知对应，可报 0day）。

    best-effort：无 llm/解析失败返回空（不误伤上报，宁可人工再看一眼）。
    """
    if llm is None:
        return []
    prompt = f"""你是漏洞去重员。判断下面的漏洞发现是否为**公开已知漏洞**的复现
（存在已分配的 CVE 编号）。注意：

- 只有当你能对应到具体 CVE 编号时才输出；不确定就输出空列表
- 相似但细节不同的实现缺陷不算复现
- 只输出 JSON：{{"cve_candidates": ["CVE-2021-44228"], "note": "一句话理由"}}
- 若确为已知漏洞复现，这很重要——把 1day 当 0day 报会损害信誉

【发现】
标题：{title}
类别：{vuln_class or "未分类"}
描述：{description[:1200]}"""
    try:
        if hasattr(llm, "call"):
            resp = await llm.call([{"role": "user", "content": prompt}], [], model)
        else:
            resp = await llm([{"role": "user", "content": prompt}], [], model, "")
        content = (resp.choices[0].message.content or "").strip()
    except Exception:
        return []
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return []
    out = []
    for c in data.get("cve_candidates") or []:
        cve = str(c).upper().strip()
        if _valid_cve(cve) and cve not in out:
            out.append(cve)
    return out[:5]
