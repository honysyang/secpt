"""B2/C3：族级打法 playbook 自动蒸馏（确定性，不调 LLM）。

数据源：`var/knowledge/settle_digests/*.json`（每意图结算时落盘的"卡点/结论"摘要）。
产物：`var/knowledge/playbooks/<family>.md`——按题族（a/b/c/d/e1/e2/f1/f2…）聚合的
"已验证路线 / 已否定路线"速查，注入 benchmark 意图 goal（B2），并同步沉淀为
一条族级经验（C3，供跨任务检索）。

设计取舍：
- 纯确定性蒸馏（标题解析 + 摘要截断），不引入额外 LLM 调用；
- 只保留最近 N 条、每条截断——playbook 是"提示词预算"的一部分，不能失控；
- 生成/注入均 best-effort，失败不影响主链路（红线：学习层不得阻断执行）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_MAX_ENTRIES = 3
_MAX_ENTRY_CHARS = 320
_BUILT = False


def _var_dir() -> Path:
    return Path(os.environ.get("SECPT_VAR", "var"))


def playbook_dir() -> Path:
    return _var_dir() / "knowledge" / "playbooks"


def family_of(code: str) -> str:
    """题族：e1-01 → e1；b-02 → b；f2-05 → f2。"""
    m = re.match(r"([a-z]+\d*)", (code or "").lower())
    return m.group(1) if m else ""


def _uc_from_title(title: str) -> str:
    m = re.search(r"通关\s+([a-z0-9-]+)", title or "")
    return m.group(1) if m else ""


def _load_digests() -> list[dict]:
    ddir = _var_dir() / "knowledge" / "settle_digests"
    out: list[dict] = []
    if not ddir.is_dir():
        return out
    for f in sorted(ddir.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        uc = _uc_from_title(str(d.get("title") or ""))
        if uc:
            d["_uc"] = uc
            d["_family"] = family_of(uc)
            out.append(d)
    return out


def build_playbooks() -> dict[str, str]:
    """按族聚合 digest → 写 playbook 文件；返回 {family: 文本}。确定性、幂等。"""
    global _BUILT
    by_fam: dict[str, list[dict]] = {}
    for d in _load_digests():
        fam = d.get("_family") or ""
        if fam:
            by_fam.setdefault(fam, []).append(d)
    out: dict[str, str] = {}
    pdir = playbook_dir()
    for fam, rows in sorted(by_fam.items()):
        # 取最近（文件顺序近似）且有实质尾部的条目；尾部包含"已验证/已否定/建议"
        picked = [r for r in rows if str(r.get("tail") or "").strip()][-_MAX_ENTRIES:]
        if not picked:
            continue
        lines = [f"# {fam} 族打法速查（自动蒸馏，来自 {len(rows)} 条历史结算摘要）", ""]
        for r in picked:
            tail = re.sub(r"\s+", " ", str(r.get("tail") or "")).strip()[:_MAX_ENTRY_CHARS]
            lines.append(f"- {r.get('_uc', '?')}（{r.get('steps', 0)} 步）：{tail}")
        text = "\n".join(lines) + "\n"
        out[fam] = text
        try:
            pdir.mkdir(parents=True, exist_ok=True)
            p = pdir / f"{fam}.md"
            if not p.is_file() or p.read_text(encoding="utf-8") != text:
                p.write_text(text, encoding="utf-8")
        except Exception:
            pass
    _BUILT = True
    return out


def _ensure_built() -> dict[str, str]:
    global _BUILT
    if _BUILT:
        return {}
    try:
        return build_playbooks()
    except Exception:
        _BUILT = True
        return {}


def playbook_for(uc: str, limit: int = 600) -> str:
    """取本题族的 playbook 注入段（无则空串）。"""
    fam = family_of(uc)
    if not fam:
        return ""
    if not _BUILT:
        _ensure_built()
    text = ""
    try:
        p = playbook_dir() / f"{fam}.md"
        if p.is_file():
            text = p.read_text(encoding="utf-8")
    except Exception:
        return ""
    text = text.strip()
    if not text:
        return ""
    return f"\n\n【族级打法（{fam}，自动蒸馏自历史结算；只作线索，需自行验证）】\n{text[:limit]}"


def distill_experiences() -> int:
    """C3：把族级 playbook 沉淀为经验条目（每族一条，已存在则跳过）。返回新增数。"""
    from secpt.learning.store import ExperienceJournal, LearnedExperience

    try:
        builds = build_playbooks()
    except Exception:
        return 0
    if not builds:
        return 0
    jr = ExperienceJournal()
    existing = {e.technique for e in jr.load() if e.technique.startswith("family_playbook_")}
    added = 0
    for fam, text in builds.items():
        tech = f"family_playbook_{fam}"
        if tech in existing:
            continue
        exp = LearnedExperience(
            title=f"{fam} 族打法速查（自动蒸馏）",
            technique=tech,
            indicators=[fam, "benchmark", "family playbook"],
            what_worked=text[:800],
            pitfalls=[],
            outcome="success",
            importance=6.0,
            task_type="benchmark",
            job_id="playbook",
        )
        try:
            if jr.append(exp):
                added += 1
        except Exception:
            continue
    return added


__all__ = ["build_playbooks", "distill_experiences", "family_of", "playbook_for"]
