"""S5/S6 编排策略声明（policy）——明确"不做的事"。

新编排模型是**并行专业链**，不是树搜索。以下概念在本阶段正式退役，
任何新代码不得重新引入：

- 不做 LLM 评分（Evaluator / EvalScore / evidence scoring）
- 不做树搜索（Scheduler 波前 / Node / Genome / 分形树）
- 不做 DeathCertificate 降级（death / degradation / exhaustion）

现有 core/evaluator/ 与 core/node/ 已在此阶段正式删除。
S6 模式收敛（2026-10-19）：单一执行编排 = frontier 意图驱动；
route_policy/direct-vs-team 路由与 SINGLE_ENTRY_MAX 随单链模式一并退役
（路由已内聚到 dag.run_task：benchmark → campaign，其余 → frontier）。

编排只做一件事：frontier 意图循环（claim → run_engine → settle），
每一步都是一个完整不可断裂的 Engine 链（单链语义 S0 红线原样保留）。

prove_goal：量化验收——settle 前核对 done_criteria 的可实测判据，
不达标不置 done（杜绝"自宣告即完成"的假成功）
"""

from __future__ import annotations

import re
from pathlib import Path

# 禁止重新引入的模块名（硬约束，供审查/测试引用）
DEPRECATED_MODULES = (
    "secpt.core.evaluator",
    "secpt.core.node",
    "secpt.core.scheduler",
    # 固定 DAG 编排器：team 路径已由 frontier 角色意图折叠（E 清理；
    # WarroomHooks 迁至 core/memory/warroom.py，勿在新代码引用旧路径）
    "secpt.core.orchestrator.specialist",
    # S6 模式收敛：多阶段编排器已折叠为 frontier stages= 意图链
    # （Stage 数据模型保留在 core/profile/loader.py，勿复活执行器）
    "secpt.core.orchestrator.stages",
    # S6 模式收敛：单链编排（attempt-2/r2/RESET 固定重试）已退役——
    # 失败恢复由 frontier 意图复核/RESET/升级强模型承担（更细粒度、更完整）
    "secpt.core.orchestrator.dag.run_single_chain",
)


# done_criteria 中的可实测判据：文件落盘类（"xxx.json 落盘" / "report.md 落盘"）
_FILE_MARKER_RE = re.compile(r"([\w./-]+\.(?:json|md|txt|log))\s*(?:落盘|存在|已生成)")


def prove_goal(
    done_criteria: str,
    answer: str,
    *,
    job_ws: Path | None = None,
) -> tuple[bool, str]:
    """P2 量化验收：settle(done) 前核对 done_criteria 的可实测判据。

    返回 (是否通过, 说明)。判据分三类：
    1. 文件落盘类：done_criteria 提到"<文件> 落盘/存在" → 实测文件在 job_ws 下存在
    2. 成果类：done_criteria 提到 flag / 答案 → answer 必须含真实 flag 形态
       （与 flagsem 同源；pentest"确认漏洞"类无 flag 判据不走此路）
    3. 无显式判据：fail-open 通过（done_criteria 通常是自然语言，不能一票否决）

    best-effort：任何异常一律 fail-open 放行（验收是纪律增强，不能反噬编排）。
    """
    try:
        crit = (done_criteria or "").strip()
        ans = (answer or "").strip()

        # 1. 文件落盘判据：逐条实测
        for fname in _FILE_MARKER_RE.findall(crit):
            if job_ws is not None:
                target = Path(job_ws) / Path(fname).name
                if not target.exists():
                    return (
                        False,
                        f"验收未通过：done_criteria 要求 {fname} 落盘，但未找到（{target}）",
                    )
            # 无 job_ws 上下文（如单元测试/外部调用）→ 无法实测，跳过该条
        # 2. 成果判据：明确要求 flag 且无"或"备选路径时 → answer 必须含真实 flag 形态。
        #    含"或"的判据（"拿到 flag 或确认漏洞"）是多路成功语义，fail-open
        #    （pentest 确认漏洞无 flag 形态也是合法成果，不一票否决）
        if re.search(r"flag", crit, re.IGNORECASE) and "或" not in crit:
            from secpt.core.orchestrator.flagsem import extract_real_flags

            if not extract_real_flags(ans):
                return False, "验收未通过：done_criteria 要求 flag/成果，但 answer 无真实 flag 形态"
        return True, "验收通过"
    except Exception as e:  # 验收失败不阻断编排（fail-open）
        return True, f"验收器异常（放行）: {e}"
