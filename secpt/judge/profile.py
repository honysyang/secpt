"""profile.toml 装配（§3.7）：judge.enabled 开关 + ground_truth 命中清单。

为什么缺文件不拒跑（拍板③）：profile 是"裁判的弹药"，缺弹药只能说明打不了
oracle——裁决降级为全部悬置（M3 R1-R3 接手），流程照走；audit 里烙一行关闭
注记（由 loop 装配层负责），诚实性由"降级必有痕"保证。坏 toml 则不同：
那是操作方笔误，静默降级会掩盖真问题，故 E_PROFILE 拒跑。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel

from secpt.errors import SecptError


class GroundTruth(BaseModel):
    """一条 oracle 命中锚：pattern 在 title/detail 里子串命中即判 verified。"""

    id: str
    pattern: str


class JudgeProfile(BaseModel):
    """裁判装配快照：enabled 开关 + ground_truth + source（溯源路径）。"""

    enabled: bool = False
    ground_truth: tuple[GroundTruth, ...] = ()
    source: str | None = None
    target: str | None = None


def load_profile(path: str | None) -> JudgeProfile:
    """读 profile.toml 装配 JudgeProfile。

    三档语义（拍板③）：
    - path 为 None 或文件不存在 → enabled=False、ground_truth 空、source=None
      （降关闭不拒跑）；
    - 文件存在但 toml 解析失败 → SecptError(code="E_PROFILE")（笔误拒跑有痕）。
    - 合法 → enabled 取 [judge].enabled（缺省 False），ground_truth 取顶层清单。
    """
    if not path:
        return JudgeProfile()
    file = Path(path)
    if not file.is_file():
        return JudgeProfile()
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, ValueError) as exc:
        raise SecptError(
            "profile.toml 解析失败",
            code="E_PROFILE",
            context={"path": str(file), "reason": str(exc)},
        ) from exc
    if not isinstance(data, dict):
        raise SecptError("profile.toml 顶层必须是表", code="E_PROFILE", context={"path": str(file)})

    judge = data.get("judge")
    enabled = bool(judge.get("enabled", False)) if isinstance(judge, dict) else False

    ground_truth: tuple[GroundTruth, ...] = ()
    raw_items = data.get("ground_truth", [])
    if not isinstance(raw_items, list):
        raise SecptError(
            "ground_truth 必须是清单",
            code="E_PROFILE",
            context={"path": str(file), "got": type(raw_items).__name__},
        )
    if enabled or raw_items:
        # enabled 才校验条目形态；但写了坏条目即报（不静默吞笔误）。
        parsed: list[GroundTruth] = []
        for item in raw_items:
            if not isinstance(item, dict) or not item.get("id") or not item.get("pattern"):
                raise SecptError(
                    "ground_truth 条目缺 id/pattern",
                    code="E_PROFILE",
                    context={"path": str(file), "item": str(item)},
                )
            parsed.append(GroundTruth(id=str(item["id"]), pattern=str(item["pattern"])))
        ground_truth = tuple(parsed)

    target_raw = data.get("target")
    target = str(target_raw) if target_raw is not None else None
    return JudgeProfile(enabled=enabled, ground_truth=ground_truth, source=str(file), target=target)


def note_for(prof: JudgeProfile) -> str:
    """装配注记一句话（loop enter() 烙 audit / web 面板同款文案）。"""
    if not prof.enabled:
        return "裁判关闭（无 profile 或 judge.enabled=false）"
    n = len(prof.ground_truth)
    src = prof.source or "?"
    return f"裁判开启 R0→R4→R1/R2/R3（profile={src}，ground_truth {n} 条）"


__all__ = [
    "GroundTruth",
    "JudgeProfile",
    "load_profile",
    "note_for",
]
