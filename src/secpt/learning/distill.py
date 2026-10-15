"""运行后经验蒸馏：transcript → LearnedExperience（LLM 提取，best-effort）。

从 var/jobs/<id>/ 的 events.jsonl + ws/transcript.json 取材：
- 成功路径：what_worked（可复用的技术路线）
- 失败/无效尝试：pitfalls（防重复踩坑）
任何一步失败（无 transcript / LLM 不可用 / 输出不可解析）都返回 None，
由调用方决定是否静默跳过。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from secpt.learning.store import ExperienceJournal, LearnedExperience

_PROMPT = """你是渗透测试经验提炼器。根据下面的任务与轨迹，提炼一条可迁移经验（不要针对具体靶场，要抽象成通用打法）。

只输出 JSON：
{{"title": "一句话标题", "technique": "技术分类(英文小写下划线)", "indicators": ["特征关键词1", "特征关键词2"], "what_worked": "行之有效的做法（200字内）", "pitfalls": ["无效尝试/坑"], "outcome": "success|fail|partial", "importance": 1-10}}

indicators：题型/入口特征标签，供同类任务启动时检索命中。用目标平台、入口或漏洞类型描述，例如：
admin bot（机器人代付/敏感操作）、xss exfil（外带通道）、sql injection、command injection、ssrf、
lfi/rfi（文件读取包含）、反序列化、auth bypass（认证绕过）、越权（idor）、crypto（密码学）、
binary pwn（二进制利用）、源码审计、内网横向。至少 3 个，越贴合目标越优先。

importance：这条经验对同类任务的价值（10=直接决定成败的打法，1=细枝末节）

【任务】
{objective}

【轨迹摘要】
{trajectory}
"""


async def _acomplete(llm, messages: list[dict], model: str) -> str:
    if hasattr(llm, "call"):
        resp = await llm.call(messages, [], model)
    else:
        resp = await llm(messages, [], model, "")
    return (resp.choices[0].message.content or "").strip()


def _extract_json(text: str) -> dict | None:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _load_job_text(job_dir: Path, max_chars: int = 6000) -> tuple[str, str]:
    """返回 (objective, trajectory)。objective 优先 task.yaml。"""
    objective = ""
    task_yaml = job_dir / "task.yaml"
    if task_yaml.is_file():
        try:
            import yaml

            objective = str(
                yaml.safe_load(task_yaml.read_text(encoding="utf-8"))["spec"]["objective"]
            )
        except Exception:
            pass

    traj_parts: list[str] = []
    transcript = None
    for cand in (job_dir / "ws" / "transcript.json", job_dir / "ws" / "ws" / "transcript.json"):
        if cand.is_file():
            transcript = cand
            break
    if transcript is not None:
        try:
            for m in json.loads(transcript.read_text(encoding="utf-8")):
                role = m.get("role")
                if role == "system":
                    continue
                content = str(m.get("content") or "")
                if m.get("tool_calls"):
                    for tc in m["tool_calls"]:
                        fn = tc.get("function") or {}
                        traj_parts.append(f"> {fn.get('name')}({str(fn.get('arguments'))[:100]})")
                elif role == "tool":
                    traj_parts.append(f"< {content[:200]}")
                elif content.strip():
                    traj_parts.append(f"{role}: {content[:300]}")
        except Exception:
            pass
    trajectory = "\n".join(traj_parts)[-max_chars:]
    return objective or job_dir.name, trajectory


def _clamp_importance(v) -> float:
    """importance 钳位 1-10（缺省/异常 → 5）。"""
    try:
        return max(1.0, min(10.0, float(v)))
    except (TypeError, ValueError):
        return 5.0


async def distill_job(
    job_id: str,
    llm=None,
    model: str = "deepseek-chat",
    jobs_dir: Path | None = None,
    journal: ExperienceJournal | None = None,
) -> LearnedExperience | None:
    """蒸馏一个 job 的经验并入库。失败返回 None（绝不抛）。"""
    from secpt.core.job.resume import jobs_dir as _default_jobs_dir

    try:
        jd = Path(jobs_dir) if jobs_dir else _default_jobs_dir()
        job_dir = jd / job_id
        objective, trajectory = _load_job_text(job_dir)
        if not trajectory.strip():
            return None

        if llm is None:
            if os.environ.get("SECPT_FAKE_LLM"):
                return None  # 假 LLM 不蒸馏（无真实经验可学）
            from secpt.runtime.llm.client import call_llm

            llm = call_llm

        text = await _acomplete(
            llm,
            [
                {
                    "role": "user",
                    "content": _PROMPT.format(objective=objective[:1500], trajectory=trajectory),
                }
            ],
            model,
        )
        data = _extract_json(text)
        if not data or not data.get("title"):
            return None

        task_type = "generic"
        task_yaml = job_dir / "task.yaml"
        if task_yaml.is_file():
            try:
                import yaml

                task_type = str(
                    yaml.safe_load(task_yaml.read_text(encoding="utf-8"))["spec"].get(
                        "task_type", "generic"
                    )
                )
            except Exception:
                pass

        exp = LearnedExperience(
            title=str(data["title"])[:120],
            technique=str(data.get("technique") or "")[:60],
            indicators=[str(x)[:40] for x in (data.get("indicators") or [])][:8],
            what_worked=str(data.get("what_worked") or "")[:400],
            pitfalls=[str(x)[:120] for x in (data.get("pitfalls") or [])][:6],
            outcome=str(data.get("outcome") or "partial"),
            importance=_clamp_importance(data.get("importance")),
            task_type=task_type,
            job_id=job_id,
        )
        jr = journal or ExperienceJournal()
        return exp if jr.append(exp) else None
    except Exception as e:
        print(f"[learn] distill {job_id} failed (ignored): {e}")
        return None
