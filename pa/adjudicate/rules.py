"""JEV 裁决规则（CONTROL_PLANE §6.4）：纯规则，可单测，无副作用。

调度器（pa/runtime/scheduler.py）只负责 sweep 编排与落账，判定本身在这里。
v0 三条规则，全部确定性：

- confirmed：证据引用 ≥2，且其中 ≥1 条核验通过且含原始输出（stdout 非空），
  且声明了证伪条件（falsify_if 非空）；
- false_positive：证据引用 ≥1 但全部无法核验（verify 不过 / 查无此证）——
  证据链断裂，排除，reason 必须写清为什么排除（给甲方的可信度证明）；
- suspected：其余（证据不足、部分引用无法核验等）。
"""

from __future__ import annotations

import hashlib
import json

from pa.errors import SecptError
from pa.store.view import StoreView

CONFIRMED_MIN_REFS = 2


def finding_id(f) -> str:
    """finding 的稳定身份：hash(claim, reproduce, asset_id) 前 12 位。

    旧实现是全内容哈希——evidence/severity 一变 id 就跟着变，合并
    （merge_finding 取最新内容）后旧裁决就成了孤儿。身份只取「这条发现
    说的是哪件事」：claim/reproduce/asset_id；evidence_refs/severity/
    falsify_if/origin 是演化字段，不参与身份。
    """
    blob = json.dumps(
        {
            "claim": f.claim,
            "reproduce": f.reproduce,
            "asset_id": f.asset_id,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def proposal_key(f) -> str:
    """单次提案的完整内容哈希（事件幂等键用）：同一提案重复落账去重，
    内容演化（补证据/改严重度）算新提案——账本保持 append-only 的原始
    提案流，合并语义在投影层（merge_finding）。"""
    blob = json.dumps(f.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def adjudicate(view: StoreView, f) -> tuple[str, str]:
    """裁决一条 finding，返回 (verdict, reason)。只读证据库，无副作用。"""
    total = len(f.evidence_refs)
    verified_raw = 0
    unverifiable = 0
    for ref in f.evidence_refs:
        try:
            ok = view.evidence_verified(ref)
        except SecptError:
            ok = False
        if not ok:
            unverifiable += 1
            continue
        content = view.evidence(ref)
        if str(content.get("stdout", "")).strip():
            verified_raw += 1
    if total >= 1 and unverifiable == total:
        return (
            "false_positive",
            f"证据链断裂：{total} 条引用全部无法核验（verify 失败），排除",
        )
    if total >= CONFIRMED_MIN_REFS and verified_raw >= 1 and f.falsify_if:
        return (
            "confirmed",
            f"证据 {total} 条、含可核验原始输出 {verified_raw} 条、已声明证伪条件",
        )
    return (
        "suspected",
        f"证据不足：refs={total}、可核验原始输出={verified_raw}、"
        f"无法核验={unverifiable}",
    )


def unlock_hint(verdict: str, f) -> str:
    """suspected 的补证指引（Adjudication.unlock）：报告与编排者据此
    知道「差什么才能升级/排除」，而不是只看到一句证据不足。"""
    if verdict != "suspected":
        return ""
    if not f.falsify_if:
        return "声明证伪条件（falsify_if）并补 1 条可核验原始输出证据"
    if len(f.evidence_refs) < CONFIRMED_MIN_REFS:
        return "补 1 条独立可核验证据（或反证）后可升级 confirmed / 降为 false_positive"
    return "补充反证（counter_evidence）后可升级 confirmed"
