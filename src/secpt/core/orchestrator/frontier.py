"""planner/frontier：意图驱动的深度优先调度（多步利用链不丢计划）。

CHECKMATE/HPTSA 的可迁移最小实现：
- planner 每轮全新会话（省 context、防漂移），只负责产意图（intent）入 frontier
- frontier 存 SQLite（按任务隔离：var/jobs/<base>/frontier.sqlite）——常驻
  todolist 跨 resume/重启持久化（比 ARTEX 内存版强，天然配 7×24）
- worker = 现有单链 Engine：领一条 intent 跑到自宣告，计划本身不依赖模型上下文记忆
- 深度优先 + 否定记账：intent 有 depth 与 attempts，失败两次即 exhausted
  （至多复核一次——加强②后为**运行时强制**：首轮失败自动回队复核，重跑时
  objective 带最新 recent_results/共享 finding）
- 资产级在途互斥（加强①）：带 asset_scope 的意图在 claim 同一事务内抢占
  asset_leases 租约——同资产意图不并发执行（此前只有 SharedStopper 事后让出），
  settle/崩溃恢复释放租约；已完成资产的等待者直接跳过结算（不重复执行）
- 任务级单编排锁（加强④）：flock var/jobs/<base>/.frontier.lock——同一任务
  并发启动第二个编排器直接拒绝（防双方 _recover_stale 互重置在途意图）
- 人机通道（加强③）：intent add/set/rm（直投/编辑/撤回，见 secpt intent --help）
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from secpt.core.orchestrator.summary import ChainResult, RunSummary  # 统一汇总契约

# 历史共享库路径（legacy）：仅作显式 `Frontier()` 无参构造的兜底（测试/工具）。
# 生产链路一律走 frontier_db_path(base)——F1 修复：意图库按任务隔离。
DEFAULT_DB = Path(os.environ.get("SECPT_VAR", "var")) / "frontier.sqlite"


def frontier_db_path(base_job_id: str) -> Path:
    """任务级意图库路径（F1）：var/jobs/<base>/frontier.sqlite。

    此前全项目共享 var/frontier.sqlite，三个后果：并发任务互相认领对方意图
    （claim 无任务过滤）、_recover_stale 把其他在跑任务的 running 意图置回
    open（双执行）、total() 终身累计吃满 max_intents 后 planner 对所有后续
    任务永久静默。按任务隔离后三者同时消失，库随 job 目录纳入既有留存/归档。
    SECPT_FRONTIER_DB 可显式覆盖（特殊部署/测试）；调用时解析（SECPT_VAR
    后设不漂移——与 situation_report 的取值口径一致）。
    """
    override = os.environ.get("SECPT_FRONTIER_DB", "")
    if override:
        return Path(override)
    from secpt.core.job.resume import jobs_dir

    return jobs_dir() / base_job_id / "frontier.sqlite"


# 复核纪律运行时强制（加强②）：首轮失败自动回队一次，第二次失败才终态
_RECHECK_ATTEMPTS = 2


def _acquire_task_lock(base: str):
    """任务级单编排锁（加强④）：flock 独占 var/jobs/<base>/.frontier.lock。

    同一任务的 frontier 并发启动直接拒绝——两个编排器共享同一 todo 库会
    各自 _recover_stale 重置对方在途意图（双执行）。崩溃自动释放（进程退出
    内核回收 flock）。Windows 无 fcntl → 返回 None（降级为不加锁）。
    """
    from secpt.core.job.resume import jobs_dir

    lock_path = jobs_dir() / base / ".frontier.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        f = open(lock_path, "w", encoding="utf-8")  # noqa: SIM115 锁需持有到编排结束
    except OSError:
        return None
    try:
        import fcntl

        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            raise RuntimeError(
                f"任务 {base} 已有 frontier 编排在运行（并发启动会互相重置在途意图）。"
                f"等待其结束后重试；确认无进程在跑时可删除 {lock_path}"
            ) from None
        with contextlib.suppress(OSError):
            f.write(str(os.getpid()))
            f.flush()
    except ImportError:
        pass  # Windows：无 fcntl，降级不加锁（best-effort）
    return f


def _release_task_lock(handle) -> None:
    if handle is None:
        return
    with contextlib.suppress(Exception):
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_UN)
    with contextlib.suppress(Exception):
        handle.close()


def settle_digest_key(title: str) -> str:
    """settle 摘要台账的指纹：sha1(title + BENCHMARK_TASK_ID)。

    带上跑分任务 id（设置了才拼）以按任务隔离——同题编码在平台新任务里复用时，
    旧任务的失败摘要不会串进来。写入方（frontier）与读取方（benchmark 重试注入）
    共用本函数，保证口径一致。
    """
    import hashlib

    raw = f"{title or ''}|{os.environ.get('BENCHMARK_TASK_ID', '')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _write_settle_digest(
    base: str, title: str, status: str, steps: int, fail_reason: str, messages: list | None
) -> None:
    """结算摘要台账（重试注入失败上下文的真相源）：按 title 指纹落盘。

    内容 = 状态/步数/失败原因 + 最后一条 assistant 正文尾部（卡点/已试路线）。
    重跑同类任务（如跑分下一轮）时由调用方按同一指纹取回注入 goal——让重试
    "带记忆换路线"，而不是从零重打（实测 19 道 exhausted 全因路线问题而非预算，
    带上下文的重试才有效）。best-effort：任何失败静默。
    """
    try:
        import json as _json

        tail = ""
        for m in reversed(messages or []):
            c = str(m.get("content") or "")
            if m.get("role") == "assistant" and c.strip():
                tail = c.strip()[:1200]
                break
        d = Path(os.environ.get("SECPT_VAR", "var")) / "knowledge" / "settle_digests"
        d.mkdir(parents=True, exist_ok=True)
        payload = {
            "title": title,
            "status": status,
            "steps": int(steps or 0),
            "fail_reason": (fail_reason or "")[:300],
            "tail": tail,
            "base": base,
            "ts": time.time(),
        }
        path = d / f"{settle_digest_key(title)}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _ingest_recon_assets(path, *, job_id: str = "") -> int:
    """把 recon 的 ws/assets.json 确定性灌入 intel 资产图（加强⑤）。

    "程序算关系，LLM 只交观察"的落地：recon→exploit 的资产传递此前是尽力
    而为（取决于模型自觉调 observe_asset），现由编排层解析清单直接入图并
    建锚——exploit 的"已知资产实体"与覆盖度不再依赖模型自觉。best-effort：
    解析/写图失败静默（记忆层不阻断主链）。返回入图条数。
    """
    try:
        import json as _json

        from secpt.core.orchestrator.goals import _infer_asset_type
        from secpt.intel.store import IntelStore

        data = _json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return 0
        st = IntelStore(Path(os.environ.get("SECPT_VAR", "var")) / "intel.sqlite")
        n = 0
        for item in data:
            if not isinstance(item, dict):
                continue
            host = str(item.get("host") or "").strip()
            if not host:
                continue
            port = item.get("port")
            value = f"{host}:{port}" if port else host
            try:
                st.observe_asset(_infer_asset_type(value), value, job_id=job_id)
                n += 1
            except Exception:
                continue
        return n
    except Exception:
        return 0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT DEFAULT '',
    done_criteria TEXT DEFAULT '',
    priority INTEGER DEFAULT 5,
    depth INTEGER DEFAULT 0,
    status TEXT DEFAULT 'open',        -- open/running/done/exhausted/blocked
    source TEXT DEFAULT 'planner',     -- planner | direct（人工直投：规划者见 direct 即暂停）
    depends_on TEXT DEFAULT '',        -- 逗号分隔 intent_id 或 fact:谓词（D2 依赖解锁）
    max_steps INTEGER DEFAULT 0,       -- 意图级步数预算（0=不限；>0 硬止损，收益递减机械化）
    subgoal TEXT DEFAULT '',            -- Goals 层：本意图服务的子目标（跨轮方向一致性的锚点）
    role TEXT DEFAULT '',               -- 角色体系：recon/exploit/verify/report（空=普通意图）
    asset_scope TEXT DEFAULT '',        -- 角色意图挂靠的资产（去重键 + claim 过滤键）
    job_id TEXT DEFAULT '',
    result TEXT DEFAULT '',
    attempts INTEGER DEFAULT 0,
    fail_reason TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intents_status ON intents(status, priority DESC, depth);
CREATE TABLE IF NOT EXISTS asset_leases (
    asset_scope TEXT PRIMARY KEY,      -- 资产维度在途互斥（加强①）：同资产意图不并发
    intent_id TEXT NOT NULL,
    acquired_at REAL NOT NULL
);
"""


@dataclass
class Intent:
    id: str
    title: str
    goal: str
    done_criteria: str
    priority: int
    depth: int
    status: str
    attempts: int
    source: str = "planner"
    depends_on: str = ""
    max_steps: int = 0
    role: str = ""
    asset_scope: str = ""


_SCOPE_LEDGERS: dict[str, object] = {}  # ③ 跨进程广播载体：按库路径缓存（见 _get_scope_ledger）


def _get_scope_ledger():
    """懒建账本实例（scope_done 广播的跨进程载体）。账本库不可用时返回 None。

    按 **库路径**（调用时解析 SECPT_VAR）缓存——此前模块级单例只建一次，
    首个调用者的 var 路径被固化，后续 SECPT_VAR 变化（测试/多实例）读到旧库，
    scope_done 标记跨测试串扰（复评修复轮：曾把"已完成跳过"放大为整意图作废）。
    """
    try:
        from secpt.core.job.resume import jobs_dir
        from secpt.core.orchestrator.ledger import ChallengeLedger

        key = str((jobs_dir().parent / "benchmark_ledger.sqlite").resolve())
        if key not in _SCOPE_LEDGERS:
            _SCOPE_LEDGERS[key] = ChallengeLedger(key)
        return _SCOPE_LEDGERS[key]
    except Exception:
        return None


def _has_ws_output(job_id: str) -> bool:
    """⑦ 进展感知续步的判据：子 job ws 有除 api.py 模板外的实质产出文件。

    有产出 = 模型已取得中间进展（写出探测脚本/报告/利用代码），只是步数耗尽
    未收尾——按"攻击路径已成型就该打穿"允许续一段预算；无产出 = 空转，
    硬止损不续。api.py 是编排层预置模板（每个 worker 首步必写），不计为进展；
    transcript.json 是引擎收尾自落盘的会话记录，同样不是模型产出（排除它，
    "空转不续"语义才真实生效——否则任何跑过的 job 都"有产出"）。
    """
    if not job_id:
        return False
    try:
        from secpt.core.job.resume import jobs_dir

        ws = jobs_dir() / job_id / "ws"
        if not ws.is_dir():
            return False
        for p in ws.rglob("*"):
            if p.is_file() and p.name not in ("api.py", "transcript.json") and p.stat().st_size > 0:
                return True
    except Exception:
        return False
    return False


_CARRY_EXCLUDE_NAMES = {"transcript.json"}  # 会覆盖续步会话，不搬运


async def _reset_handoff_summary(result: object, obj: str, llm: object) -> str:
    """RESET 首条上下文：上轮会话的 handoff 摘要（设计 §3.2 四件套之一）。

    优先 LLM 六段摘要（与 rollover 同一 summarizer，对上轮 transcript 生成）；
    LLM 不可用/失败时退化为最后一条 assistant 正文尾部（确定性，与 settle
    摘要台账同口径）。best-effort：任何失败都不阻断 RESET 主链。
    """
    msgs = list(getattr(result, "messages", None) or [])
    try:
        if msgs and llm is not None:
            from secpt.core.context.summarize import get_summarizer

            api_key = os.environ.get("DEEPSEEK_API_KEY", "")
            summarizer = get_summarizer(llm, model="deepseek-chat", api_key=api_key)
            h = await summarizer(obj, msgs)
            if h and str(h).strip():
                return str(h).strip()
    except Exception:
        pass
    tail = ""
    for m in reversed(msgs):
        c = str(m.get("content") or "")
        if m.get("role") == "assistant" and c.strip():
            tail = c.strip()[:1200]
            break
    return tail or "（上轮无正文产出）"


def _reset_negative_list(fr: object, intent: object) -> str:
    """RESET 四件套之四：已验证否定的路线（recent_results 的 exhausted 聚合）。"""
    try:
        rows = fr.recent_results(4)  # duck-typing：Frontier.recent_results
        lines = [
            f"- [{r.get('status')}] {r.get('title', '')}: {r.get('fail_reason', '')}"
            for r in rows
            if r.get("status") in ("exhausted", "blocked") and r.get("fail_reason")
        ]
        return "\n".join(lines) if lines else "（暂无可复用否定记录）"
    except Exception:
        return "（否定清单不可用）"


def _carry_ws_artifacts(old_job: str, new_job: str) -> str:
    """P1-1：把上一段（-r2 之前）工作区的产出文件复制到续步工作区，返回文件清单。

    续步用新 job_id（独立事件流、不覆盖旧 checkpoint），工作区也随之独立；不搬运
    产物的话"继续原路径打穿"就无从谈起。排除 transcript.json（覆盖续步会话）与
    outputs/（bash 日志，体积大且非解题产物）。返回 `- 名称 (大小)` 行清单供注入。
    """
    lines: list[str] = []
    try:
        from secpt.core.job.resume import jobs_dir

        old_ws = jobs_dir() / old_job / "ws"
        if not old_ws.is_dir():
            return "（无）"
        new_ws = jobs_dir() / new_job / "ws"
        new_ws.mkdir(parents=True, exist_ok=True)
        for p in sorted(old_ws.rglob("*")):
            if not p.is_file() or p.name in _CARRY_EXCLUDE_NAMES:
                continue
            rel = p.relative_to(old_ws)
            if rel.parts and rel.parts[0] == "outputs":
                continue
            dest = new_ws / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(Exception):
                shutil.copy2(p, dest)
                lines.append(f"- {rel} ({p.stat().st_size} bytes)")
    except Exception:
        return "（复制失败，可重新探测）"
    return "\n".join(lines) if lines else "（无）"


class SharedStopper:
    """per-asset 完成广播（阶段三：治"通关后仍乱猜"）。

    团队模式多 worker 并行时，同 asset_scope 已有意图 done（拿到 flag/确认漏洞），
    在途的同类意图（exploit 变体 / verify）应让出收尾，而不是继续无意义深挖。
    线程安全：_run_one 经 asyncio.to_thread 在各线程执行，广播/查询跨线程。
    ③ 跨进程扩展：除进程内集合外，还轮询账本 scope_done 表——其他进程置位的
    完成广播同样可见（ARTEXT"状态在数据库，模型读状态"的让出语义落地）。
    """

    def __init__(self, cross: object | None = None) -> None:
        self._lock = threading.Lock()
        self._done_assets: set[str] = set()
        self._done_intents: set[str] = set()  # 自己结算过的不算让出对象
        self._cross = cross  # 具备 is_scope_done(key) 的账本载体（缺省 None 仅进程内）

    def mark_done(self, asset_scope: str, iid: str) -> None:
        """结算 done 时广播：同资产的后续在途意图应让出。"""
        if not asset_scope:
            return
        with self._lock:
            self._done_assets.add(asset_scope)
            self._done_intents.add(iid)

    def should_yield(self, asset_scope: str, iid: str) -> bool:
        """该意图是否应让出：同资产已被别的意图完成（进程内或跨进程账本），且不是它自己。

        用于**在途让出提示**（_YieldHook：只影响 steer，不致命）。
        账本 scope_done 是 var 级持久标记（跨任务/跨时间仍命中）——"整个意图
        作废"这类强动作请用 should_yield_local（仅本次运行的广播）。
        """
        if not asset_scope:
            return False
        with self._lock:
            if asset_scope in self._done_assets and iid not in self._done_intents:
                return True
        # 跨进程查询（锁外做 IO，账本异常时 fail-open 不让出）
        if self._cross is not None:
            try:
                if self._cross.is_scope_done(asset_scope):
                    return True
            except Exception:
                pass
        return False

    def should_yield_local(self, asset_scope: str, iid: str) -> bool:
        """仅进程内广播判定（不含跨进程账本）：用于"跳过执行"的强动作。

        持久账本命中（历史任务曾完成该资产）不足以作废本任务意图——新任务
        重新授权同一资产时应当照跑；只有**本次运行**中刚完成的资产才跳过。
        """
        if not asset_scope:
            return False
        with self._lock:
            return asset_scope in self._done_assets and iid not in self._done_intents


class _YieldHook:
    """包装内部 hooks 链：on_step 查询 SharedStopper，命中则返回 steer 消息。

    engine 的 on_step 返回非空串时会经 on_user_message(kind='steer') 注入 user 消息，
    引导在途 LLM 主动收尾（对齐 worker.py:_CancelHook 的包装模式）。只注入一次防刷屏；
    inner 已有导引（watchdog nudge 等）时优先，不覆盖。
    """

    def __init__(
        self, inner: object, stopper: SharedStopper, asset_scope: str, self_iid: str
    ) -> None:
        self._inner = inner
        self._stopper = stopper
        self._asset = asset_scope
        self._self_iid = self_iid
        self._sent = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def on_step(self, steps: int, elapsed: float) -> str | None:
        steer = None
        inner_step = getattr(self._inner, "on_step", None)
        if inner_step is not None:
            steer = inner_step(steps, elapsed)
        if steer:
            return steer  # 内部已有导引（空转 nudge 等）优先
        if not self._sent and self._stopper.should_yield(self._asset, self._self_iid):
            self._sent = True
            return (
                "【协作广播】同资产的另一 worker 已完成该目标（拿到 flag 或确认漏洞），"
                "请立即停止继续探索，把已取得的结论整理后输出 ANSWER: <当前结论>。"
            )
        return None


def _aggregate_sub_events(
    base: str,
    sub_jobs: list[str],
    chains: list[dict],
    flags: list[str],
    *,
    aggregated: set[str] | None = None,
    final: bool = True,
) -> None:
    """P0-2 + 增量：把子 job 的 events.jsonl 聚合到父 job（frontier 各意图跑在独立子 job）。

    没有这一步，父 job 目录无 events.jsonl → export_log/evaluate_job 完全失效
    （tsec-run-0522：日志导出与评判功能整体失灵的根因）。
    调用模式：
    - 增量（final=False）：意图 settle 即聚合该子 job——运行中可 watch、
      进程崩溃不丢失、天然幂等（aggregated 集合去重，重复子 job 跳过）
    - 收尾（final=True）：补漏未聚合的子 job + 追加 summary 事件（人读入口）
    - 子事件逐行透传，job_id 改写为父 job_id，payload 前缀标注 subjob 来源
    best-effort：任何异常都不炸主循环（可观测层不能反噬编排层）。
    """
    import json as _json

    from secpt.core.job.resume import jobs_dir

    seen = aggregated if aggregated is not None else set()
    try:
        fresh = [s for s in sub_jobs if s and s not in seen]
        if not fresh and not final:
            return  # 增量调用无新子 job：跳过（防重复聚合）
        parent_dir = jobs_dir() / base
        parent_dir.mkdir(parents=True, exist_ok=True)
        parent_events = parent_dir / "events.jsonl"
        seq = 0
        if parent_events.is_file():
            with open(parent_events, encoding="utf-8") as rf:
                seq = sum(1 for _ in rf)
        with open(parent_events, "a", encoding="utf-8") as out:
            for sub in fresh:
                sub_path = jobs_dir() / sub / "events.jsonl"
                if not sub_path.is_file():
                    continue  # 子 job 事件未生成（engine 异常早退）→ 下次收尾再试
                with open(sub_path, encoding="utf-8") as rf:
                    for line in rf:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ev = _json.loads(line)
                        except _json.JSONDecodeError:
                            continue
                        seq += 1
                        ev["seq"] = seq
                        ev["job_id"] = base
                        payload = ev.get("payload")
                        if isinstance(payload, dict):
                            ev["payload"] = {"subjob": sub, **payload}
                        out.write(_json.dumps(ev, ensure_ascii=False) + "\n")
                seen.add(sub)  # 已聚合标记（幂等：下次调用跳过）
            if final:
                seq += 1
                out.write(
                    _json.dumps(
                        {
                            "seq": seq,
                            "job_id": base,
                            "ts": time.time(),
                            "round": 0,
                            "type": "round_end",
                            "payload": {
                                "n_messages": 0,
                                "sub_jobs": list(seen),
                                "chains": len(chains),
                                "flags": flags,
                                "note": "frontier 收尾聚合（子 job 事件已并入父事件流）",
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    except Exception as e:  # 可观测层失败不阻断编排
        print(f"[frontier] 父事件流聚合失败（忽略）: {e}")


def _queue_verify_if_needed(fr: Frontier, base: str) -> bool:
    """团队模式：exploit 已有成果且存在未确认 finding → 确定性预置独立复验意图。

    验证纪律确定性化：report_finding 上报即 inferred（findings/tool.py 语义），
    confirmed 只允许独立 PoC 复验升级——这里把 verify 从"依赖 planner 自觉补产"
    改为确定性触发（与 report 触发同模式），杜绝 P1 缺口：verify 不产 → confirmed
    恒空 → 报告恒空。
    幂等：verify 意图存在过即不再派。
    """
    with fr._conn() as c:
        done_exploits = [
            r["id"]
            for r in c.execute(
                "SELECT id FROM intents WHERE role='exploit' AND status='done' ORDER BY created_at"
            ).fetchall()
        ]
    if not done_exploits or fr.has_role_intent("verify"):
        return False
    # 存在本任务的 inferred finding 才值得复验（避免无 finding 空跑 verify）
    try:
        from secpt.findings.store import FindingStore

        has_inferred = any(
            str(f.job_id or "").startswith(base) and f.status == "inferred"
            for f in FindingStore(
                # 调用时 SECPT_VAR 解析（模块级 DEFAULT_DB 是 import 期快照，会漂移）
                Path(os.environ.get("SECPT_VAR", "var")) / "findings.sqlite"
            ).list(limit=1000)
        )
    except Exception:
        has_inferred = False
    if not has_inferred:
        return False
    db_path = str(Path(os.environ.get("SECPT_VAR", "var")) / "findings.sqlite")
    with fr._conn() as c:
        row = c.execute("SELECT MAX(depth) d FROM intents").fetchone()
        next_depth = int(row["d"] or 0) + 1
    fr.add(
        "独立复验（verify）",
        goal=(
            f"读取漏洞库 {db_path} 中 job_id 以 {base} 开头且 status='inferred' 的 "
            "finding，逐条用独立 PoC 重新复现（新请求/新环境，不复用原上报路径），"
            "复现成功把该 finding 升级为 confirmed，复现失败标记 false_positive。"
        ),
        done_criteria="所有 inferred findings 给出复验结论",
        priority=8,
        depth=next_depth,
        depends_on=",".join(done_exploits),
        role="verify",
    )
    return True


_RECURSE_MARKER = "已递归展开"


def _pick_intent_model(role: str, default: str | None) -> str | None:
    """§4.3 验证异构：verify 意图可用 SECPT_VERIFY_MODEL 指定异构模型（缺省=主模型）。

    独立子 job（会话独立 ✓）+ 容器独立已有；异构模型把"同模型同盲区"概率打掉
    （MAPTA/XBOW 对照：validation 独立成角色）。只影响 verify 角色，其余角色
    恒用主模型。
    """
    if role == "verify":
        v = os.environ.get("SECPT_VERIFY_MODEL", "").strip()
        if v:
            return v
    return default


def retro(fr: Frontier, *, base: str = "") -> str:
    """复盘会（设计 §4.1）：确定性阶段路由（零 LLM，纯 SQL/计数）。

    把"planner 该不该再规划"从模型状态里拿出来——阶段由状态定，planner 只管内容：
    - converge：全部 subgoal settled（与主循环现有收敛判据同口径）
    - recurse ：存在"复核后仍 exhausted 的路线型失败"（step_exceeded/wallclock，
                未展开过、资产尚无任何 confirmed finding）→ 主循环做失败驱动
                局部递归（§4.2，ADaPT 语义）
    - continue：其余（常规 planner 轮）
    """
    try:
        if fr.all_subgoals_settled():
            return "converge"
    except Exception:
        pass
    if _find_recurse_candidate(fr, base=base) is not None:
        return "recurse"
    return "continue"


def _find_recurse_candidate(fr: Frontier, *, base: str = "") -> dict | None:
    """找一条可局部递归展开的父意图：路线型失败 + 终态穷尽 + 未展开过。

    路线型失败两种形态：
    - 复核后仍败：fail_reason 含 step_exceeded/wallclock 且"（尝试 2/3 次）"
      （首轮失败会进自动复核，复核后的 exhausted 才是真路线穷尽）；
    - 意图级步数预算硬止损："超意图步数预算"（带 max_steps 的意图不进复核、
      一次即终态——跑分 exhausted 题群正是这种形态，正是递归要救的人群）。
    """
    try:
        with fr._conn() as c:
            rows = c.execute(
                """SELECT id,title,asset_scope,depth,done_criteria,fail_reason FROM intents
                   WHERE status='exhausted'
                   ORDER BY updated_at DESC LIMIT 8"""
            ).fetchall()
        for r in rows:
            why = str(r["fail_reason"] or "")
            if _RECURSE_MARKER in why:
                continue  # 每父意图最多展开一层（防递归爆炸）
            if "超意图步数预算" in why:
                pass  # 硬止损：带 max_steps 的意图无复核轮，一次即终态
            elif ("step_exceeded" in why or "wallclock_exceeded" in why) and (
                "（尝试 2 次）" in why or "（尝试 3 次）" in why
            ):
                pass  # 复核后仍败的路线型失败
            else:
                continue  # error/budget/首轮失败等：展开无用
            asset = str(r["asset_scope"] or "").strip()
            if asset and _asset_has_confirm(base, asset):
                continue  # 资产已有 confirmed 产出：不是路线问题，展开无用
            return dict(r)
    except Exception:
        return None
    return None


def _asset_has_confirm(base: str, asset: str) -> bool:
    """该资产是否已有 confirmed finding（跨子 job，job_id 前缀匹配）。"""
    try:
        from secpt.findings.store import FindingStore

        for f in FindingStore(Path(os.environ.get("SECPT_VAR", "var")) / "findings.sqlite").list(
            status="confirmed", limit=1000
        ):
            if not str(f.job_id or "").startswith(base):
                continue
            hay = f"{f.target}\n{f.title}\n{f.description}"
            if asset in hay:
                return True
    except Exception:
        return False
    return False


# §4.4 演化提案：finding 值里的资产形态（URL / IPv4），用于"题面外资产"判定
_DRIFT_TOKEN_RE = re.compile(r"https?://[^\s'\"]+|\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _detect_goal_drift(base: str, objective: str) -> str | None:
    """题面外资产集中（>50% confirmed 无关 且 ≥2 条）→ 返回演化提案文本。

    侦察推翻题面假设时的确定性信号：已确认发现集中在题面资产之外，说明目标
    理解可能错了（如题面给的是入口 A，真实可利用面在旁站 B）。纯计数、零 LLM。
    """
    try:
        from secpt.findings.store import FindingStore

        rows = [
            f
            for f in FindingStore(
                Path(os.environ.get("SECPT_VAR", "var")) / "findings.sqlite"
            ).list(status="confirmed", limit=1000)
            if str(f.job_id or "").startswith(base)
        ]
    except Exception:
        return None
    if len(rows) < 2:
        return None
    unrelated: list[object] = []
    for f in rows:
        val = f.target or f.title
        toks = set(_DRIFT_TOKEN_RE.findall(val))
        # URL 归一化到 origin（scheme://host）再比对——题面常只给入口域名，
        # 全路径 containment 会把同站 finding 误判为题面外资产
        norm = set()
        for t in toks:
            m = re.match(r"(https?://[^/]+)", t)
            norm.add(m.group(1) if m else t)
        if toks and not any(t in (objective or "") for t in norm):
            unrelated.append(f)
    if len(unrelated) >= 2 and len(unrelated) * 2 > len(rows):
        lines = [f"- {(f.target or f.title)[:120]}" for f in unrelated[:4]]
        return "已确认发现集中在题面资产之外（疑似目标理解偏差）：\n" + "\n".join(lines)
    return None


def _queue_goal_revision(
    fr: Frontier, *, base: str, objective: str, proposal: str, mode: str
) -> bool:
    """演化提案意图入队 + 审批处置（§4.4，复用 intercept 审批基建）。

    mode=ask（默认）：提案进 intercept pending 队列等人工审批（扩 scope 的提案
    永远走这条，ROE 不可自动化）；mode=auto：纯细化类自动批准；mode=off 由
    调用方先行过滤。幂等：已有未结算的 revision 意图不再重复入队。
    """
    try:
        with fr._conn() as c:
            n = c.execute(
                "SELECT COUNT(*) n FROM intents WHERE source='revision'"
                " AND status IN ('open','running','blocked')"
            ).fetchone()["n"]
        if int(n) > 0:
            return False
        fr.add(
            "目标演化提案（待审批）",
            goal=(
                f"【提案依据】{proposal}\n"
                f"【当前目标】{objective[:200]}\n"
                "处置：人工审批通过后由上层重渲染目标/细化 scope；否决则维持原目标。"
            ),
            done_criteria="提案经人工审批处置（批准细化/否决维持）",
            priority=10,
            depth=0,
            source="revision",
        )
        if mode == "ask":
            try:
                from secpt.governance.intercept import Interceptor

                icp = Interceptor()
                pid = f"icp-goal-{uuid.uuid4().hex[:8]}"
                with icp._conn() as c:
                    c.execute(
                        """INSERT INTO intercept_pending
                           (id,job_id,target,preview,rule_id,rule_name,status,ts)
                           VALUES (?,?,?,?,?,?, 'pending', ?)""",
                        (
                            pid,
                            base,
                            "goal_revision",
                            proposal[:200],
                            -1,
                            "目标演化提案",
                            time.time(),
                        ),
                    )
                print(
                    f"[frontier] ⏸ 目标演化提案待审批 #{pid}：\n{proposal[:200]}\n"
                    f"（批准：secpt approve {pid} / 拒绝：secpt deny {pid}）"
                )
                with contextlib.suppress(Exception):
                    from secpt.runtime.notify import notify

                    notify(
                        "approval_pending",
                        f"secpt 目标演化提案待审批 #{pid}",
                        f"{proposal[:200]}\n批准：secpt approve {pid} / 拒绝：secpt deny {pid}",
                    )
            except Exception as e:
                print(f"[frontier] 提案审批登记失败（忽略，意图仍在队列）: {e}")
        return True
    except Exception:
        return False


def _expand_recurse(fr: Frontier, *, base: str, max_intents: int) -> int:
    """失败驱动局部递归（§4.2）：编排层直接产结构化子意图（不经 planner LLM）。

    只在失败时展开（ADaPT 核心）；两条子意图走正常租约/依赖/复核链。返回入队条数
    （0 = 无可展开 / 达意图上限）。planner 也可产同类意图——双轨并存，这条保证
    "planner 忘了展开"时系统仍会动。
    """
    cand = _find_recurse_candidate(fr, base=base)
    if cand is None:
        return 0
    try:
        with fr._conn() as c:
            total = int(c.execute("SELECT COUNT(*) n FROM intents").fetchone()["n"])
        if total + 2 > max_intents:
            return 0  # 意图预算守门：递归不突破调用方上限
        pid = cand["id"]
        title = str(cand["title"] or "")
        asset = str(cand["asset_scope"] or "").strip()
        depth = int(cand["depth"] or 0)
        parent_criteria = str(cand.get("done_criteria") or "").strip()
        asset_txt = asset or "父意图目标"
        why = str(cand["fail_reason"] or "")[:80]
        sub1 = fr.add(
            f"重勘表层：{title}"[:80],
            goal=(
                f"对 {asset_txt} 枚举此前路线未覆盖的表层（读源码/robots/API 目录/参数面）。"
                f"父意图「{title}」路线型失败（{why}）——只找父路线没试过的新面，"
                "不重复已验证否定的路线。"
            ),
            done_criteria="产出 ≥1 条新端点或明确证明无",
            priority=7,
            depth=depth + 1,
            source="retro_recurse",
            role="recon",
            asset_scope=asset,
        )
        fr.add(
            f"换路线再打：{title}"[:80],
            goal=(
                f"基于重勘（依赖 {sub1}）的新端点，对 {asset_txt} 换一条与父路线"
                f"（{why}）完全不同的攻击路线，达成父意图原目标：{title}。"
            ),
            done_criteria=parent_criteria
            or f"达成父意图原目标：{title}（父判据缺失，按原目标验收）",
            priority=8,
            depth=depth + 1,
            source="retro_recurse",
            role="exploit",
            asset_scope=asset,
            depends_on=sub1,
        )
        with fr._conn() as c:
            c.execute(
                "UPDATE intents SET fail_reason = fail_reason || ? WHERE id = ?",
                (f" [{_RECURSE_MARKER}]", pid),
            )
        return 2
    except Exception:
        return 0


class Frontier:
    """意图队列（SQLite 持久化 todolist）。"""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            self._migrate(c)
            self._recover_stale(c)

    def _recover_stale(self, c: sqlite3.Connection) -> None:
        """崩溃恢复：进程被杀遗留的 running 意图置回 open（可重新认领）。

        任务级库（frontier_db_path）下每个库对应**一个任务**的一次编排上下文，
        running 只可能来自该任务先前被杀的进程——init 时统一放回可领队列，
        避免永久卡死阻塞收敛（stale running 会计入 pending 覆盖度）。
        （F1 前共享 var/frontier.sqlite 时该假设不成立：会把其他运行中任务的
        在途意图置回 open，被第二个编排器双执行。）
        加强①：running 意图持有的资产租约一并回收——否则资产被"幽灵意图"
        锁死（意图回 open 了，租约还在，该资产永远领不到）。
        """
        with contextlib.suppress(sqlite3.Error):
            c.execute(
                "DELETE FROM asset_leases WHERE intent_id IN"
                " (SELECT id FROM intents WHERE status='running')"
            )
            c.execute(
                "UPDATE intents SET status='open', updated_at=? WHERE status='running'",
                (time.time(),),
            )

    def _migrate(self, c: sqlite3.Connection) -> None:
        """老库补列（CREATE IF NOT EXISTS 不会给已存在的表加新列）。"""
        cols = {r["name"] for r in c.execute("PRAGMA table_info(intents)")}
        if "source" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN source TEXT DEFAULT 'planner'")
        if "depends_on" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN depends_on TEXT DEFAULT ''")
        if "max_steps" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN max_steps INTEGER DEFAULT 0")
        if "subgoal" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN subgoal TEXT DEFAULT ''")
        if "role" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN role TEXT DEFAULT ''")
        if "asset_scope" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN asset_scope TEXT DEFAULT ''")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def add(
        self,
        title: str,
        *,
        goal: str = "",
        done_criteria: str = "",
        priority: int = 5,
        depth: int = 0,
        source: str = "planner",
        depends_on: str = "",
        subgoal: str = "",
        max_steps: int = 0,  # 意图级步数预算（0=不限；>0 硬止损）
        role: str = "",  # 角色体系：recon/exploit/verify/report（空=普通意图）
        asset_scope: str = "",  # 挂靠资产（去重键 + claim 过滤键）
    ) -> str:
        iid = f"int-{uuid.uuid4().hex[:8]}"
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO intents (id,title,goal,done_criteria,priority,depth,status,
                                        source,depends_on,subgoal,max_steps,role,asset_scope,
                                        created_at,updated_at)
                   VALUES (?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?)""",
                (
                    iid,
                    title[:200],
                    goal[:1000],
                    done_criteria[:500],
                    int(priority),
                    int(depth),
                    source,
                    depends_on[:300],
                    subgoal[:300],
                    int(max_steps),
                    role[:20],
                    asset_scope[:300],
                    now,
                    now,
                ),
            )
        # ⑧ 双图锚点（ARTEX"两张图通过锚点关联"）：带资产的意图落探索图并挂靠
        #    该资产，使"资产 → 测过哪些路径"反查成立（intents 在 frontier 库、
        #    探索图在 intel 库，经 anchors 表关联）。无资产意图无可挂靠锚点 → 不落图
        #    （避免无锚噪声节点）。best-effort：图写入失败不阻断意图入队。
        if asset_scope or subgoal:
            with contextlib.suppress(Exception):
                from secpt.core.orchestrator.goals import _infer_asset_type
                from secpt.intel.store import IntelStore

                st = IntelStore(Path(os.environ.get("SECPT_VAR", "var")) / "intel.sqlite")
                aid = (
                    st.observe_asset(_infer_asset_type(asset_scope), asset_scope, job_id="frontier")
                    if asset_scope
                    else None
                )
                if subgoal:
                    st.record_goal(subgoal)
                if aid is not None:
                    st.add_node(
                        "intent",
                        f"[{role or 'gen'}] {title}",
                        anchor_assets=[aid],
                    )
        return iid

    def has_open_direct(self) -> bool:
        """存在未完成的直投意图 → 规划者暂停（人工接管模式，ARTEX 模式②）。"""
        with self._conn() as c:
            n = c.execute(
                "SELECT COUNT(*) n FROM intents WHERE source='direct'"
                " AND status IN ('open','running')"
            ).fetchone()["n"]
        return int(n) > 0

    def list_all(self, *, status: str | None = None, limit: int = 50) -> list[dict]:
        sql, args = "SELECT * FROM intents WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY priority DESC, created_at LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def deps_satisfied(self, intent: Intent) -> bool:
        """依赖解锁：depends_on 为空 → 满足；否则逐项检查 intent done / fact 谓词。"""
        raw = intent.depends_on
        if not raw:
            return True
        for dep in raw.split(","):
            dep = dep.strip()
            if not dep:
                continue
            if dep.startswith("fact:"):
                if not self._fact_exists(dep[5:]):
                    return False
            else:
                with self._conn() as c:
                    row = c.execute("SELECT status FROM intents WHERE id=?", (dep,)).fetchone()
                if row is None or row["status"] != "done":
                    return False
        return True

    def _fact_exists(self, predicate: str) -> bool:
        """fact 谓词形如 'credential:admin'（intel 图节点前缀匹配）。"""
        try:
            from secpt.intel.store import IntelStore

            st = IntelStore(Path(os.environ.get("SECPT_VAR", "var")) / "intel.sqlite")
            return bool(st.recall(predicate, limit=1))
        except Exception:
            return False

    def claim_next(self, role: str | None = None) -> Intent | None:
        """领下一条：priority DESC → depth DESC（深度优先）→ 先创建先领。

        role 指定时只认领该角色（或未定角色的老意图）——角色 worker 拓扑：
        声明确认自己角色后，认领互斥由原子 UPDATE 保证，不抢其他角色的活。
        跨进程原子性：BEGIN IMMEDIATE 先取写锁再做 SELECT+UPDATE——deferred
        事务下 SELECT 不加写锁，两个进程可能同时选中同一行再各自 UPDATE
        成功（双认领、attempts 双计）；持写锁后该序列不可交错。
        """
        role_filter = "AND (role=? OR role='')" if role else ""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            # 加强①：资产租约——带 asset_scope 的意图在**同一事务内**抢占租约，
            # 他人持有的资产一律跳过。此前同资产意图可被两个 worker 并发执行
            # （只有 SharedStopper 的事后让出），登录态/有状态靶场会互扰。
            row = c.execute(
                f"""SELECT * FROM intents WHERE status='open' {role_filter}
                   AND (asset_scope='' OR asset_scope NOT IN
                        (SELECT asset_scope FROM asset_leases))
                   ORDER BY priority DESC, depth DESC, created_at LIMIT 1""",
                (role,) if role else (),
            ).fetchone()
            if row is None:
                return None
            cur = c.execute(
                "UPDATE intents SET status='running', attempts=attempts+1, updated_at=?"
                " WHERE id=? AND status='open'",
                (time.time(), row["id"]),
            )
            if cur.rowcount != 1:
                return None  # 持写锁下不应发生；防御性放弃（commit 空更新无害）
            _scope = str(row["asset_scope"] or "")
            if _scope:
                c.execute(
                    "INSERT OR REPLACE INTO asset_leases (asset_scope, intent_id, acquired_at)"
                    " VALUES (?,?,?)",
                    (_scope, row["id"], time.time()),
                )
        d = dict(row)
        return Intent(
            id=row["id"],
            title=row["title"],
            goal=row["goal"],
            done_criteria=row["done_criteria"],
            priority=row["priority"],
            depth=row["depth"],
            status="running",
            attempts=row["attempts"] + 1,
            # sqlite3.Row 的 `in` 按值迭代不按键——必须 dict 化后 get（老库缺列兼容）
            source=d.get("source", "planner"),
            depends_on=d.get("depends_on", "") or "",
            max_steps=int(d.get("max_steps", 0) or 0),
            role=d.get("role", "") or "",
            asset_scope=d.get("asset_scope", "") or "",
        )

    def find_duplicate(self, role: str, asset_scope: str) -> bool:
        """同 asset_scope+role 已有未结算意图 → True（去重：防多轮规划重复派同一资产）。"""
        if not role or not asset_scope:
            return False
        with self._conn() as c:
            n = c.execute(
                """SELECT COUNT(*) n FROM intents
                   WHERE role=? AND asset_scope=? AND status IN ('open','running')""",
                (role, asset_scope),
            ).fetchone()["n"]
        return int(n) > 0

    def refund_attempt(self, iid: str) -> None:
        """依赖未满足被放回 blocked：回退 claim 的 attempts 消耗（未真正执行）。"""
        with self._conn() as c:
            c.execute(
                "UPDATE intents SET attempts = MAX(attempts-1, 0), updated_at=? WHERE id=?",
                (time.time(), iid),
            )

    def reopen_ready(self) -> int:
        """把依赖已满足的 blocked 意图重新置 open（依赖解锁后恢复可领）。

        依赖未满足的意图在 claim 时置 blocked（不空耗 attempts），
        当被依赖的意图 done / fact 落图后，这里把它们放回可领队列。
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM intents WHERE status='blocked' AND source != 'revision'"
                " ORDER BY priority DESC, depth DESC LIMIT 20"
            ).fetchall()
        n = 0
        for row in rows:
            d = dict(row)
            intent = Intent(
                id=d["id"],
                title=d["title"],
                goal=d["goal"],
                done_criteria=d["done_criteria"],
                priority=d["priority"],
                depth=d["depth"],
                status="blocked",
                attempts=d["attempts"],
                source=d.get("source", "planner"),
                depends_on=d.get("depends_on", "") or "",
            )
            if self.deps_satisfied(intent):
                with self._conn() as c2:
                    c2.execute(
                        "UPDATE intents SET status='open', updated_at=? WHERE id=?",
                        (time.time(), d["id"]),
                    )
                n += 1
        return n

    def settle(self, iid: str, *, status: str, result: str = "", fail_reason: str = "") -> None:
        assert status in ("done", "exhausted", "blocked", "open")
        with self._conn() as c:
            c.execute(
                "UPDATE intents SET status=?, result=?, fail_reason=?, updated_at=? WHERE id=?",
                (status, result[:2000], fail_reason[:500], time.time(), iid),
            )
            # 加强①：任何结算路径都释放该意图的资产租约（含 blocked 回退与
            # 复核回队——租约只随"在途"存在，不随状态残留）
            c.execute("DELETE FROM asset_leases WHERE intent_id=?", (iid,))

    def lease_holder(self, asset_scope: str) -> str | None:
        """当前持有该资产租约的意图 id（可观测/测试用）；无则 None。"""
        if not asset_scope:
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT intent_id FROM asset_leases WHERE asset_scope=?", (asset_scope,)
            ).fetchone()
        return str(row["intent_id"]) if row else None

    # ---------------- 人机通道：意图编辑/撤回（加强③） ----------------

    _UPDATABLE = ("title", "goal", "done_criteria", "priority", "max_steps")

    def update(self, iid: str, **fields) -> bool:
        """改意图（标题/目标/判据/优先级/步数预算）。仅 open/blocked 可改——
        running/done/exhausted 不动（防改在途语义与历史）。返回是否生效。"""
        cols = {k: v for k, v in fields.items() if k in self._UPDATABLE and v is not None}
        if not cols:
            return False
        sets = ", ".join(f"{k}=?" for k in cols)
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE intents SET {sets}, updated_at=?"
                " WHERE id=? AND status IN ('open','blocked')",
                (*cols.values(), time.time(), iid),
            )
            return cur.rowcount > 0

    def remove(self, iid: str) -> bool:
        """撤回意图（跑偏即撤）。仅 open/blocked 可删；running/done/exhausted 拒绝。"""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM intents WHERE id=? AND status IN ('open','blocked')", (iid,)
            )
            return cur.rowcount > 0

    def count_open(self) -> int:
        with self._conn() as c:
            return int(
                c.execute("SELECT COUNT(*) n FROM intents WHERE status='open'").fetchone()["n"]
            )

    def total(self) -> int:
        with self._conn() as c:
            return int(c.execute("SELECT COUNT(*) n FROM intents").fetchone()["n"])

    def has_role_intent(self, role: str, *, statuses: tuple[str, ...] | None = None) -> bool:
        """存在该角色的意图（可限定状态）——report 触发判重：报告一次就够。"""
        sql, args = "SELECT COUNT(*) n FROM intents WHERE role=?", [role]
        if statuses:
            marks = ",".join("?" * len(statuses))
            sql += f" AND status IN ({marks})"
            args.extend(statuses)
        with self._conn() as c:
            return int(c.execute(sql, args).fetchone()["n"]) > 0

    def recent_results(self, limit: int = 5) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT id,title,status,result,fail_reason FROM intents
                   WHERE status IN ('done','exhausted','blocked') AND result != ''
                   ORDER BY updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def situation_report(self, *, limit: int = 6) -> dict:
        """结构化态势：planner 的决策原材料（对齐 ARTEX 双图注入）。

        比文本摘要多的三样：intel 图最新事实/发现（跨任务记忆）、
        未触达资产（覆盖度缺口）、否定记录（防重复踩坑已有）。
        """
        report: dict = {
            "open_intents": self.open_summary(limit),
            "recent_results": self.recent_results(limit),
            "subgoal_coverage": self.subgoal_coverage(),
        }
        try:
            from secpt.intel.store import IntelStore

            # 调用时读 SECPT_VAR（模块级 DEFAULT_DB 是 import 期快照，测试/多实例会漂移）
            st = IntelStore(Path(os.environ.get("SECPT_VAR", "var")) / "intel.sqlite")
            with st._conn() as c:
                rows = c.execute(
                    """SELECT id,type,content FROM nodes
                       WHERE type IN ('fact','finding','hint') AND COALESCE(cold,0)=0
                       ORDER BY ts DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            report["recent_facts"] = [
                {"id": r["id"], "type": r["type"], "content": str(r["content"])[:200]} for r in rows
            ]
            report["untested_assets"] = [
                {"id": a["id"], "value": str(a["value"])[:100]}
                for a in st.untested_assets(atype="endpoint", limit=5)
            ]
            # 已有资产实体集（含 goal-seed 种子）：planner 产意图必须挂靠这里的实体
            report["known_assets"] = [{"value": str(a["value"])[:100]} for a in st.assets(limit=30)]
        except Exception:
            report["recent_facts"] = []
            report["untested_assets"] = []
            report["known_assets"] = []
        return report

    def open_summary(self, limit: int = 10) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT id,title,priority,depth FROM intents
                   WHERE status='open' ORDER BY priority DESC, depth DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def subgoal_coverage(self, limit: int = 10) -> list[dict]:
        """Goals 层覆盖度：按子目标聚合意图结算状态（planner 决策原材料）。

        每个子目标给出 done/exhausted/open 计数——planner 据此判断
        "哪些方向已证、哪些没测完"，跨轮方向一致性从文本记忆变结构记忆。
        """
        with self._conn() as c:
            rows = c.execute(
                """SELECT subgoal,
                          SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
                          SUM(CASE WHEN status='exhausted' THEN 1 ELSE 0 END) AS exhausted,
                          SUM(CASE WHEN status IN ('open','running','blocked')
                              THEN 1 ELSE 0 END) AS pending
                   FROM intents WHERE subgoal != ''
                   GROUP BY subgoal ORDER BY pending DESC, done DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["done"] = int(d["done"] or 0)
            d["exhausted"] = int(d["exhausted"] or 0)
            d["pending"] = int(d["pending"] or 0)
            out.append(d)
        return out

    def all_subgoals_settled(self) -> bool:
        """覆盖度驱动收敛判定：所有子目标下意图均已结算（done/exhausted）。

        存在 subgoal 分组且每个分组都无未结算意图 → True。
        没有任何 subgoal 时返回 False（退回 idle_rounds 空转逻辑，向后兼容）。
        """
        with self._conn() as c:
            row = c.execute(
                """SELECT COUNT(*) n FROM (
                       SELECT subgoal FROM intents WHERE subgoal != ''
                       GROUP BY subgoal
                       HAVING SUM(CASE WHEN status IN ('open','running','blocked')
                                       THEN 1 ELSE 0 END) > 0
                   )"""
            ).fetchone()
            total = c.execute(
                "SELECT COUNT(DISTINCT subgoal) n FROM intents WHERE subgoal != ''"
            ).fetchone()
        return int(total["n"]) > 0 and int(row["n"]) == 0


_PLANNER_PROMPT = """你是渗透任务的 planner。根据【总目标】【近期结果】【当前待办】产出下一步意图。

纪律（必须遵守）：
1. 深度优先：优先产出能推进当前最深利用链的意图，不要浅层铺开
2. 否定记账：近期结果里已证伪的路线不要重复派意图（至多复核一次）
3. 每条意图必须可判定：done_criteria 写清"怎样算这条做完了"
4. 最多产 {max_new} 条；没有值得做的就产空列表（任务自然收敛）
5. 子目标记账：每条意图可用可选字段 "subgoal" 归属到某个子目标；优先派发
   【子目标覆盖度】里 pending=0 且 done=0 的未测方向；某子目标已全部 done 就别再派
6. 角色挂靠（必须）：每条意图带 "role"（recon/exploit/verify/report）与 "asset_scope"——
   asset_scope 必须取自【已有资产实体】；不在实体集内的意图作废（不派发）
7. 去重：同 asset_scope+role 的意图已存在（见【当前待办】）就不要再派

【总目标】
{objective}

【近期结果】
{results}

【当前待办】
{opens}

【图记忆·最新事实/发现（跨任务）】
{facts}

【覆盖度缺口·未触达资产（优先派这里）】
{untested}

【已有资产实体（asset_scope 必须从这里选）】
{entities}

【子目标覆盖度】
{coverage}

只输出 JSON：[{{"title": "...", "goal": "...", "done_criteria": "...",
"priority": 1-10, "depth": {next_depth}, "subgoal": "...",
"role": "recon|exploit|verify|report", "asset_scope": "已有资产实体中的一项"}}]"""


async def plan_round(
    objective: str,
    frontier: Frontier,
    llm,
    *,
    model: str = "deepseek-chat",
    max_new: int = 3,
    next_depth: int = 0,
) -> int:
    """planner 新会话产意图。解析失败/无 llm 返回 0（调度继续，不阻塞）。"""
    situation = frontier.situation_report()
    prompt = _PLANNER_PROMPT.format(
        max_new=max_new,
        objective=objective[:1500],
        results=json.dumps(situation["recent_results"], ensure_ascii=False)[:1200] or "（无）",
        opens=json.dumps(situation["open_intents"], ensure_ascii=False)[:600] or "（无）",
        next_depth=next_depth,
        facts=json.dumps(situation.get("recent_facts", []), ensure_ascii=False)[:900] or "（无）",
        untested=json.dumps(situation.get("untested_assets", []), ensure_ascii=False)[:400]
        or "（无）",
        entities=json.dumps(situation.get("known_assets", []), ensure_ascii=False)[:600]
        or "（无）",
        coverage=json.dumps(situation.get("subgoal_coverage", []), ensure_ascii=False)[:500]
        or "（无）",
    )
    try:
        if hasattr(llm, "call"):
            resp = await llm.call([{"role": "user", "content": prompt}], [], model)
        else:
            resp = await llm([{"role": "user", "content": prompt}], [], model, "")
        content = (resp.choices[0].message.content or "").strip()
    except Exception:
        return 0
    start, end = content.find("["), content.rfind("]")
    if start < 0 or end <= start:
        return 0
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return 0
    n = 0
    # 资产挂靠强制：实体集（含 goal-seed 种子），不在集内的角色意图直接丢弃
    known = {a["value"] for a in situation.get("known_assets", [])}
    for item in data[:max_new]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        role = str(item.get("role") or "").strip()
        asset_scope = str(item.get("asset_scope") or "").strip()
        # 空想意图防线：声明角色的意图必须挂靠已知实体，否则丢弃（不派发）
        if role and asset_scope and asset_scope not in known:
            continue
        # 去重：同 asset_scope+role 已存在（open/running）→ 不派（防重复派同一资产）
        if role and asset_scope and frontier.find_duplicate(role, asset_scope):
            continue
        subgoal = str(item.get("subgoal") or "").strip()
        frontier.add(
            title,
            goal=str(item.get("goal") or ""),
            done_criteria=str(item.get("done_criteria") or ""),
            priority=int(item.get("priority", 5)),
            depth=int(item.get("depth", next_depth)),
            subgoal=subgoal,
            role=role,
            asset_scope=asset_scope,
        )
        if subgoal:
            # Goals 层：子目标落探索图（幂等；best-effort，失败不阻断派发）
            with contextlib.suppress(Exception):
                from secpt.intel.store import IntelStore

                IntelStore(Path(os.environ.get("SECPT_VAR", "var")) / "intel.sqlite").record_goal(
                    subgoal
                )
        n += 1
    return n


def build_intent_objective(
    global_objective: str, intent: Intent, recent: list[dict], *, base: str = ""
) -> str:
    recent_txt = "\n".join(
        f"- [{r['status']}] {r['title']}: {r['result'][:200]}" for r in recent[:4]
    )
    # 角色体系：recon/exploit/verify/report 意图追加角色化阶段提示（对齐 specialist 的
    # objective_extra 语义），profile 渲染链路原样复用
    role_stage = ""
    if getattr(intent, "role", ""):
        from secpt.core.orchestrator.roles import role_stage as _rs

        role_stage = "\n\n" + _rs(intent.role, getattr(intent, "asset_scope", "") or "")
    # 跨资产情报：exploit/verify 注入本任务已确认漏洞（P2 情报共享）——同目标不同
    # 资产的漏洞模式/凭据路径通常可复用，避免各资产 worker 重复挖同一类洞
    shared = ""
    if base and getattr(intent, "role", "") in ("exploit", "verify"):
        try:
            from secpt.findings.store import FindingStore

            confirmed = [
                f
                for f in FindingStore(
                    # 调用时 SECPT_VAR 解析（模块级 DEFAULT_DB 是 import 期快照，会漂移）
                    Path(os.environ.get("SECPT_VAR", "var")) / "findings.sqlite"
                ).list(limit=500)
                if f.status == "confirmed" and str(f.job_id or "").startswith(base)
            ]
            if confirmed:
                _lines = ["", "【已验证漏洞·跨资产情报（优先复用而非重挖）】"]
                for f in confirmed[:6]:
                    _lines.append(
                        f"- [{f.severity}] {f.title} @ {f.target}: "
                        f"{(f.reproduction or f.description or '')[:120]}"
                    )
                shared = "\n".join(_lines)
        except Exception:
            shared = ""
    return f"""【总目标】
{global_objective}

【当前意图】{intent.title}
目标：{intent.goal}
完成判据：{intent.done_criteria or "拿到该意图的结论即可"}{role_stage}

【近期相关结果（避免重复踩坑）】
{recent_txt or "（无）"}{shared}

要求：只推进当前意图，完成判据满足后以 ANSWER: <结论> 收尾。"""


async def run_frontier(
    objective: str,
    *,
    profile: str = "hunter",
    base_job_id: str | None = None,
    max_rounds: int = 20,
    max_intents: int = 60,  # 清单型调用方（benchmark）传预置数 → 关闭 planner；
    # 探索型入口（hunt）保持缺省让 planner 多轮产意图
    llm=None,
    model: str | None = None,  # 缺省 SECPT_MODEL_PRIMARY > deepseek-chat
    planner_llm=None,  # 缺省与 worker 同模型；可分开（强模型规划+便宜模型执行）
    frontier: Frontier | None = None,
    max_workers: int = 1,  # D2.2 并发 worker 数（ARTEX 默认 3；>1 需事件唤醒）
    wake_queue: object | None = None,  # D2.3 外部触发队列（settle 时 put intent_id）
    stop_on_first_flag: bool = True,  # 单题/狩猎：拿到第一个 flag 即整体收敛；
    # 跑分（benchmark）传 False，持续解完所有已入队题目
    role_plan: bool = False,  # 团队模式：预置 recon→exploit×N 角色意图骨架（折叠 Specialist DAG）
    default_assets: list[str] | None = None,  # 团队模式资产兜底（goal 拆解无 asset_scope 时用入口）
    on_started: object | None = None,  # P0-2：意图「真正开跑」回调 intent->None（如置 running）
    on_settled: object | None = None,  # ② 结算回调 intent->None：编排层确定性收尾（如 close 容器）
    scope_ledger: object | None = None,  # ③ 跨进程广播载体（ChallengeLedger，缺省懒建同库实例）
) -> dict:
    """意图驱动主循环。返回汇总（chains/flags/wall）。

    模式语义：
    - 队列中存在未完成的 direct（人工直投）意图 → 规划者暂停，只执行 direct，
      全部 direct 完成且无新 direct → 结束（ARTEX 模式②）；此后恢复正常规划
    - D2.2：max_workers>1 时并发跑多个 claim→run→settle 循环（原子认领）
    - role_plan=True（团队模式）：按 GoalSpec.asset_scope 预置 recon→exploit 意图骨架，
      worker 按角色认领并行推进；verify/report 由 planner 按依赖补产
    """
    import asyncio

    from secpt.cli.engine_runner import resolve_models, run_engine

    model, _fb = resolve_models(model, None)

    # llm 缺省时解析生产 LLM——planner 与 worker 共用（懒升级路径此前传 None 导致
    # planner 静默死亡：plan_round 捕获 AttributeError 返回 0 意图）
    if llm is None or planner_llm is None:
        from secpt.cli.engine_runner import _prod_llm

        _prod = _prod_llm()
        llm = llm or _prod
        planner_llm = planner_llm or _prod

    # 目标拆解层（阶段一）：objective 非结构化（未经 render_goal_system）且非 benchmark 时
    # 先拆解再规划；已结构化则幂等跳过（solve 已拆过的场景不重复调 LLM）
    from secpt.core.orchestrator.goals import (
        decompose_objective,
        is_rendered,
        render_goal_system,
        seed_goal_assets,
    )

    goal = None
    if profile != "benchmark" and not is_rendered(objective):
        goal = await decompose_objective(objective, llm=llm, model=model)
        seed_goal_assets(goal)
        objective = render_goal_system(goal)

    # F1：base 先定（意图库按任务隔离），库即 var/jobs/<base>/frontier.sqlite——
    # 并发任务互不可见；resume 同 base 续接同一 todolist
    base = base_job_id or f"hunt-{uuid.uuid4().hex[:8]}"
    # 加强④：同任务单编排锁——第二个并发实例直接拒绝（防双方 _recover_stale
    # 互重置在途意图）。异常路径由文件对象回收自动释放 flock（CPython 语义）
    _lock = _acquire_task_lock(base)
    fr = frontier or Frontier(frontier_db_path(base))

    # 团队模式（role_plan）：折叠 Specialist 固定 DAG 为角色意图骨架——
    # recon 1 条（role=recon）+ 每资产 1 条 exploit（role=exploit, depends_on=recon）。
    # 多 worker 按角色并行认领；verify/report 由 planner 按依赖补产
    if role_plan:
        team_assets = list(goal.asset_scope) if goal is not None else []
        if not team_assets:
            # 已 rendered 的 objective（solve 拆解过）：从结构化文本提取资产范围
            from secpt.core.orchestrator.goals import asset_scope_from_text

            team_assets = asset_scope_from_text(objective)
        if not team_assets and default_assets:
            team_assets = list(default_assets)
        if team_assets:
            recon_id = fr.add(
                "资产侦察（recon）",
                goal="枚举目标资产/端口/服务/技术栈",
                done_criteria="产出结构化资产清单",
                priority=9,
                depth=0,
                role="recon",
                asset_scope=",".join(team_assets)[:300],
            )
            for a in team_assets:
                if fr.find_duplicate("exploit", a):
                    continue  # 同资产 exploit 已存在 → 不重复预置
                fr.add(
                    f"利用 {a}",
                    goal=f"针对 {a} 做定向利用",
                    done_criteria="拿到 flag 或确认漏洞",
                    priority=7,
                    depth=1,
                    depends_on=recon_id,
                    role="exploit",
                    asset_scope=a,
                )
            max_workers = max(max_workers, min(len(team_assets), 4))  # exploit 并行认领
            stop_on_first_flag = False  # 团队模式：持续推进所有资产，不因单资产收敛

    chains: list[dict] = []
    flags: list[str] = []
    chains_lock = asyncio.Lock()
    stopper = SharedStopper(scope_ledger or _get_scope_ledger())  # 完成广播（进程内+账本跨进程）
    done_events: list[dict] = []  # 完成事件收集（settle done，供调用方聚合）
    sub_jobs: list[str] = []  # 子任务 job_id（父 job 事件流聚合用，P0-2）
    aggregated: set[str] = set()  # ④ 已增量聚合的子 job（幂等去重）
    idle_rounds = 0
    t0 = time.time()
    # 事件唤醒队列：外部传入则共用（D2.3 外部触发），否则本地队列——
    # worker 结算/心跳都会 put，主循环据此立即重估（不等 60s 心跳饿死）
    wake_q: asyncio.Queue = wake_queue if wake_queue is not None else asyncio.Queue()

    async def _maybe_plan() -> int:
        """规划者轮：人工接管（direct 未完成）时暂停；否则产意图。"""
        nonlocal idle_rounds
        if fr.has_open_direct():
            return 0  # 模式②：规划者暂停
        if fr.total() >= max_intents or idle_rounds >= 2:
            return 0
        with fr._conn() as c:
            row = c.execute("SELECT MAX(depth) d FROM intents").fetchone()
            max_depth = int(row["d"] or 0)
        n_new = await plan_round(
            objective,
            fr,
            planner_llm,
            model=model,
            next_depth=max_depth + (1 if fr.total() else 0),
        )
        idle_rounds = 0 if n_new else idle_rounds + 1
        return n_new

    async def _run_one(intent: Intent) -> str | None:
        """执行单条意图（含⑦进展感知续步）并结算。返回 answer（有成果时非空）。"""
        # report 角色：确定性聚合（不耗 LLM 步数），落盘报告后结算
        if getattr(intent, "role", "") == "report":
            return await _run_report(intent)
        # revision 角色（§4.4 演化提案）：确定性处置——不烧 LLM 步数，登记后
        # 挂起等人工审批（ask 模式的审批记录在 _queue_goal_revision 已落 intercept 队列）
        if getattr(intent, "source", "") == "revision":
            fr.settle(
                intent.id,
                status="blocked",
                result="目标演化提案已登记",
                fail_reason="等待人工审批（secpt approve/deny 对应 intercept pending 记录）",
            )
            async with chains_lock:
                chains.append(
                    ChainResult(
                        intent=intent.title,
                        role="revision",
                        asset_scope="",
                        status="blocked",
                        steps=0,
                        depth=intent.depth,
                        source=intent.source,
                    )
                )
            with contextlib.suppress(Exception):
                wake_q.put_nowait(intent.id)
            return None
        # 加强①配套：同资产已被完成（**本次运行**的进程内广播）→ 跳过执行直接
        # 结算 done（省一整轮意图成本）。注意用 local 判定：持久账本里的
        # 历史 scope_done 不足以作废本任务意图（新任务重测同一资产应照跑）。
        # 租约只防并发，这一层防"等待租约期间目标已被本次运行拿下"
        _scope0 = getattr(intent, "asset_scope", "") or ""
        if (
            _scope0
            and getattr(intent, "role", "") in ("exploit", "verify")
            and stopper.should_yield_local(_scope0, intent.id)
        ):
            fr.settle(intent.id, status="done", result="同资产已完成（协作广播），跳过重复执行")
            async with chains_lock:
                chains.append(
                    ChainResult(
                        intent=intent.title,
                        role=getattr(intent, "role", "") or "",
                        asset_scope=_scope0,
                        status="done",
                        steps=0,
                        depth=intent.depth,
                        source=intent.source,
                    )
                )
                done_events.append(
                    {
                        "iid": intent.id,
                        "status": "done",
                        "role": intent.role,
                        "asset_scope": _scope0,
                    }
                )
            with contextlib.suppress(Exception):
                wake_q.put_nowait(intent.id)
            return None
        # P0-2：意图「真正开始执行」→ 回调编排层置 running（claim 只表示"已预留"，
        # 二者语义分离，账本 running 数才与真实在途容器数一致）
        if on_started is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(on_started, intent)
        # 意图级步数预算：硬止损（收益递减机械化）——意图预算优先于全局 profile
        _budget = getattr(intent, "max_steps", 0)
        # 团队模式 exploit/verify：挂 per-asset 完成广播让出钩子——
        # 同资产已被其他 worker 完成时，engine 注入协作广播引导收尾（治"通关后仍乱猜"）
        hook_factory = None
        if getattr(intent, "role", "") in ("exploit", "verify") and intent.asset_scope:

            def _make_yield_hook(inner, _s=stopper, _a=intent.asset_scope, _i=intent.id):
                return _YieldHook(inner, _s, _a, _i)

            hook_factory = _make_yield_hook
        _obj = build_intent_objective(objective, intent, fr.recent_results(4), base=base)
        _job = f"{base}-i{intent.id[-4:]}"

        def _exec(obj: str = _obj, job: str = _job, budget: int = _budget) -> object:
            kw = {"max_steps": budget} if budget > 0 else {}
            return run_engine(
                objective=obj,
                profile=profile,
                job_id=job,
                llm=llm,
                model_primary=_pick_intent_model(getattr(intent, "role", ""), model),
                hook_factory=hook_factory,
                # G1 目标授权：意图资产即该子任务授权目标（无资产意图不启用 scope）
                scope_entries=[intent.asset_scope] if intent.asset_scope else None,
                **kw,
            )

        result = await asyncio.to_thread(_exec)
        # 子任务登记（P0-2）：父 job 无 events.jsonl（frontier 各意图跑在独立
        # 子 job 里），导出/评判全失效——settle 时增量聚合 + 收尾补漏
        if result.job_id:
            async with chains_lock:
                sub_jobs.append(result.job_id)
        # ⑦ 进展感知续步：预算打满但 ws 已有实质产出（攻击路径成型、只差收尾）→
        #    再续一段同预算（新 job_id -r2 独立事件流，不覆盖首段）。只续一次防无限续命；
        #    空转（无产出）不续，硬止损语义不变
        if result.status == "step_exceeded" and _budget > 0 and _has_ws_output(result.job_id or ""):
            _job2 = f"{_job}-r2"
            # P1-1：续步必须承接产物——新 job_id 是独立工作区，若只换 id 不搬文件，
            # "继续原路径打穿"就是空话（1053 实跑：r2 ws 全空，模型只能重建 api.py 重探测）。
            # 复制旧 ws 产出（排除 transcript/outputs）到 r2 ws，并把真实清单注入 objective。
            _manifest = _carry_ws_artifacts(result.job_id or "", _job2)
            _obj2 = (
                _obj + f"\n\n【进展感知续步（编排层注入）】上一段 {_budget} 步已产出中间成果，"
                f"已复制到本工作区（清单如下），本轮继续原攻击路径打穿，不要推倒重来：\n{_manifest}"
            )
            result2 = await asyncio.to_thread(_exec, _obj2, _job2, _budget)
            if result2.job_id:
                async with chains_lock:
                    sub_jobs.append(result2.job_id)
            if getattr(result2, "flag", ""):
                # 续跑拿到成果：采用续跑结果（事件已在 sub_jobs，资产检查保留首段 job_id）
                _jid_keep = result.job_id
                result = result2
                result.job_id = _jid_keep
            else:
                # 续跑也未收尾：累计步数（仍走 exhausted 止损）
                with contextlib.suppress(Exception):
                    result.steps = int(result.steps) + int(result2.steps)
        # ---- RESET（设计 §3）：stalled → 换会话重建（restart beats patching）----
        # 触发：三档止损判 stalled（策略级卡死，区别于预算墙）+ ws 有实质产出。
        # 与 r2 续步（同会话链延续）不同，与 rollover（同会话换链、保 prefix
        # cache）也不同：RESET 换会话——新 job_id 独立事件流/工作区，从外部状态
        # （handoff 四件套）重建上下文，丢掉带着旧假设越跑越脏的会话。
        # 门：首尝才 RESET（attempts 门 = 每意图上限 1 次，防死循环）；空转
        # （ws 无产出）不重建，直接走下方 exhausted 语义。
        if (
            result.status == "stalled"
            and not getattr(result, "flag", "")
            and _budget > 0
            and int(getattr(intent, "attempts", 1) or 1) <= 1
            and _has_ws_output(result.job_id or "")
        ):
            _job_rs = f"{_job}-rs"
            _manifest = _carry_ws_artifacts(result.job_id or "", _job_rs)
            _handoff = await _reset_handoff_summary(result, _obj, llm)
            _negs = _reset_negative_list(fr, intent)
            _obj_rs = (
                _obj + "\n\n【RESET 重建（编排层注入）】上一会话已策略级卡死（stalled），"
                "本会话从外部状态重建上下文，严禁原样重跑旧路线：\n"
                f"1. 上轮 handoff 摘要：\n{_handoff}\n"
                f"2. 已复制工作区产物（继续用，不要推倒重来）：\n{_manifest}\n"
                f"3. 已验证否定的路线（勿重复踩坑）：\n{_negs}\n"
                "基于以上状态选一条与上轮不同的路线推进，尽快以 ANSWER: 收尾。"
            )
            result_rs = await asyncio.to_thread(_exec, _obj_rs, _job_rs, _budget)
            if result_rs.job_id:
                async with chains_lock:
                    sub_jobs.append(result_rs.job_id)
            try:
                from secpt.runtime.metrics import counter_inc

                counter_inc(
                    "loop_reset_total", outcome="recovered" if result_rs.flag else "exhausted"
                )
            except Exception:
                pass
            if getattr(result_rs, "flag", ""):
                # RESET 拿到成果：采用其结果（资产检查保留首段 job_id）
                _jid_keep = result.job_id
                result = result_rs
                result.job_id = _jid_keep
            else:
                # RESET 也未收尾：累计步数，按 RESET 后状态走下方 settle 分支
                # （stalled 不在复核名单 → 直接 exhausted，不再重建，防死循环）
                with contextlib.suppress(Exception):
                    result.steps = int(result.steps) + int(result_rs.steps)
                result.status = result_rs.status
        answer = result.flag or ""
        if answer:
            status, why = "done", ""
            # P2 量化验收：done_criteria 明确要求 flag（无"或"备选路径）时，
            # answer 必须含真实 flag 形态才置 done——杜绝"自宣告即完成"的假成功
            # （含"或"的判据是多路成功语义，pentest"确认漏洞"不被一票否决）
            if "或" not in (intent.done_criteria or ""):
                from secpt.core.orchestrator.policy import prove_goal

                ok, why_v = prove_goal(intent.done_criteria, answer)
                if not ok:
                    status, why = "exhausted", why_v
        elif getattr(intent, "role", "") == "recon" and result.status not in (
            "error",
            "failed",
            "budget_exceeded",
        ):
            # 侦察意图：正常结束即 done——依赖它的 exploit 意图据此解锁（recon
            # 角色语义）。校验资产清单是否落盘：未产出则 result 显式
            # 标注，让 exploit 的 recent_results 可见"侦察空转"（不阻断解锁，避免
            # 误伤确实无资产可枚举的目标）
            status, why = "done", "侦察完成"
            try:
                from secpt.core.job.resume import jobs_dir

                _af = jobs_dir() / (result.job_id or "") / "ws" / "assets.json"
                if not (_af.is_file() and "[" in _af.read_text(encoding="utf-8", errors="replace")):
                    why = "侦察完成但未产出资产清单（ws/assets.json 缺失或空）"
                else:
                    # 加强⑤：清单确定性灌入 intel 资产图（不依赖模型自觉 observe_asset）
                    _n_ing = _ingest_recon_assets(_af, job_id=base)
                    if _n_ing:
                        why = f"侦察完成（资产入图 {_n_ing} 条）"
            except Exception:
                pass
        elif getattr(intent, "max_steps", 0) > 0 and result.status == "step_exceeded":
            status, why = "exhausted", f"超意图步数预算（{intent.max_steps} 步硬止损）"
        elif result.status == "error":
            status, why = "blocked", "engine error"
        elif (
            result.status in ("step_exceeded", "wallclock_exceeded", "failed")
            and intent.attempts < _RECHECK_ATTEMPTS
        ):
            # 加强②：复核纪律运行时强制——首轮失败自动回队重跑一次（重跑时
            # build_intent_objective 会带上最新 recent_results/共享 finding，
            # 期间落下的新事实自然进入复核上下文）；第二次失败才终态 exhausted。
            # 硬止损类（意图级步数预算 / 引擎 error / 预算超限）不进复核，防烧钱循环。
            status, why = "open", f"{result.status}（首轮失败，自动复核一次）"
        else:
            status, why = "exhausted", f"{result.status}（尝试 {intent.attempts} 次）"
        fr.settle(
            intent.id, status=status, result=answer or f"steps={result.steps}", fail_reason=why
        )
        fr.reopen_ready()  # 本条结算后立即解锁依赖已满足的 blocked 意图（事件驱动，不等轮询）
        # 完成广播（双通道）：进程内 SharedStopper（快路径）+ 账本 scope_done
        # （③ 跨进程可见——其他 worker 进程轮询同一账本即可让出同资产在途意图）。
        # 加强①时序：settle 已释放资产租约，广播必须紧跟其后（在下面 on_settled
        # 等 await 之前置位）——同资产等待者的认领与"已完成跳过"之间不留 await 窗口
        if status == "done" and getattr(intent, "role", "") in ("exploit", "verify"):
            stopper.mark_done(intent.asset_scope, intent.id)
        scope_key = getattr(intent, "asset_scope", "") or ""
        if status == "done" and scope_key:
            try:
                _sl = scope_ledger or _get_scope_ledger()
                _sl.mark_scope_done(scope_key)
            except Exception:
                pass  # 广播通道失败不阻断结算（进程内 stopper 仍生效）
        # ② 结算回调：编排层确定性收尾（benchmark close 容器 / 其他资源回收）。
        #    回调失败不阻断结算——收尾是 best-effort，任务状态以 settle 为准
        if on_settled is not None:
            try:
                import asyncio as _aio

                await _aio.to_thread(on_settled, intent)
            except Exception as e:
                print(f"[frontier] on_settled 回调失败（忽略）: {e}", flush=True)
        async with chains_lock:
            chains.append(
                ChainResult(
                    intent=intent.title,
                    role=getattr(intent, "role", "") or "",
                    asset_scope=getattr(intent, "asset_scope", "") or "",
                    status=status,
                    steps=result.steps,
                    depth=intent.depth,
                    source=intent.source,
                )
            )
            if status == "done":
                done_events.append(
                    {
                        "iid": intent.id,
                        "status": status,
                        "role": getattr(intent, "role", "") or "",
                        "asset_scope": getattr(intent, "asset_scope", "") or "",
                    }
                )
            # ④ 增量事件聚合：本意图的子 job 事件即刻并入父事件流——
            #    运行中可 watch、进程崩溃不丢失（幂等：aggregated 去重）
            if result.job_id:
                _aggregate_sub_events(
                    base, sub_jobs, chains, flags, aggregated=aggregated, final=False
                )
            # ⑤ settle 摘要台账：重跑任务按 title 指纹取回"上次卡点"注入 goal（best-effort）
            _write_settle_digest(
                base, intent.title, status, result.steps, why, getattr(result, "messages", None)
            )
        # 事件唤醒主循环（无条件：结算变化必须立刻可见，防 60s 心跳饿死）
        with contextlib.suppress(Exception):
            wake_q.put_nowait(intent.id)
        # 侦察角色的 ANSWER 是侦察摘要（非 flag），不计入成果 flags；
        # 其他角色（exploit/verify）自宣告的 flag 照常返回
        if getattr(intent, "role", "") == "recon":
            return None
        return answer or None

    async def _run_report(intent: Intent) -> None:
        """report 角色：确定性聚合 findings/资产/否定记录 → 落盘 report.md。

        纯聚合无攻击面，代码生成比 LLM 稳定且不耗步数；落盘路径即 ANSWER 语义
        （role_stage 的"报告落盘后输出报告路径"由 write_report 返回值承担）。
        聚合失败不炸调度：标记 exhausted，主循环正常收敛。
        """
        status, why, result_txt = "done", "", ""
        try:
            from secpt.report.aggregate import build_report, render_markdown, write_report

            report = build_report(job_id=base, objective=objective, frontier=fr)
            md = render_markdown(report)
            rpath = write_report(md, job_id=base)
            result_txt = f"报告落盘 {rpath.name}"
        except Exception as e:
            status, why = "exhausted", f"报告聚合失败: {e}"
        fr.settle(intent.id, status=status, result=result_txt, fail_reason=why)
        async with chains_lock:
            chains.append(
                ChainResult(
                    intent=intent.title,
                    role="report",
                    asset_scope="",
                    status=status,
                    steps=0,
                    depth=intent.depth,
                    source=intent.source,
                )
            )
            if status == "done":
                done_events.append(
                    {"iid": intent.id, "status": status, "role": "report", "asset_scope": ""}
                )
        with contextlib.suppress(Exception):
            wake_q.put_nowait(intent.id)
        return None

    def _queue_report_if_needed(fr: Frontier, objective: str) -> bool:
        """团队模式收敛前：exploit 已有成果且 report 未派发 → 预置 report 意图。

        返回 True 表示已预置（worker 认领执行聚合；执行完再走收敛判定）。
        幂等：report 意图存在过即不再派（has_role_intent 不限状态）。
        """
        with fr._conn() as c:
            done_exploit = c.execute(
                "SELECT COUNT(*) n FROM intents WHERE role='exploit' AND status='done'"
            ).fetchone()["n"]
        if (
            int(done_exploit) == 0
            or fr.has_role_intent("report")
            or fr.has_role_intent("verify", statuses=("open", "running", "blocked"))
        ):
            return False  # verify 未完成前不派 report（报告只聚合 confirmed）
        with fr._conn() as c:
            row = c.execute("SELECT MAX(depth) d FROM intents").fetchone()
            next_depth = int(row["d"] or 0) + 1
        fr.add(
            "聚合渗透报告（report）",
            goal="汇总已验证 finding/资产/否定记录，生成结构化渗透报告",
            done_criteria="report.md 落盘",
            priority=10,
            depth=next_depth,
            role="report",
        )
        return True

    stop = False
    stop_ev = asyncio.Event()

    async def _worker_loop() -> None:
        nonlocal stop
        while not stop_ev.is_set():
            intent = fr.claim_next()
            if intent is None:
                fr.reopen_ready()  # 无 open 可领 → 解锁依赖已满足的 blocked 意图
                await asyncio.sleep(0.5)
                continue
            if not fr.deps_satisfied(intent):  # D2.1 依赖未满足 → blocked（不空耗 attempts）
                fr.settle(intent.id, status="blocked", fail_reason="依赖未满足")
                fr.refund_attempt(intent.id)
                await asyncio.sleep(0.5)
                continue
            ans = await _run_one(intent)
            if ans:
                # 成果语义收窄（P0-1）：flags 只采信真实 flag 形态，ANSWER 全文（结论摘要/
                # benchmark 结算串）不计入跑分，防 unique_code 前缀污染。
                # stop_on_first_flag 的"收敛"语义仍以 ans 存在为准（pentest"确认漏洞"也要收敛），
                # flags 数组本身只收 real flag——非 flag 形态的成果仍保留在 chains/result 可追溯。
                from secpt.core.orchestrator.flagsem import extract_real_flags

                real = extract_real_flags(ans)
                if real:
                    flags.extend(v for v in real if v not in flags)
                if stop_on_first_flag:
                    stop = True
                    stop_ev.set()  # 拿到成果即整体收敛（单题/狩猎模式）

    workers = [asyncio.create_task(_worker_loop()) for _ in range(max_workers)]

    # 事件驱动 planner：worker settle / 心跳 唤醒（对齐 ARTEX"数据库变化唤醒"）
    idle_rounds = 0
    rounds = 0
    while rounds < max_rounds and not stop:
        if fr.has_open_direct():
            # 模式②：只执行 direct；队列清空且无 running → 结束
            if fr.count_open() == 0:
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake_q.get(), timeout=2.0)
            continue
        starving = fr.count_open() < max_workers
        # 是否还能产新意图（意图数达上限 / 规划连续两轮无产出 → 收敛点）
        can_plan = fr.total() < max_intents and idle_rounds < 2
        if can_plan and (starving or fr.count_open() == 0):
            await _maybe_plan()
            rounds += 1
        if fr.count_open() == 0:
            if fr.total() == 0 and rounds == 0:
                continue  # 首盘：等 worker 空转也无妨，继续 plan
            # 团队模式：verify（存在未确认 finding）优先于 report（exploit 有成果）
            # 确定性触发；verify 完成前不派 report（报告只聚合 confirmed）
            if role_plan:
                if _queue_verify_if_needed(fr, base):
                    continue  # worker 认领 verify 意图执行（独立 PoC 复验）
                if _queue_report_if_needed(fr, objective):
                    continue  # worker 认领 report 意图执行；执行完再走收敛判定
            if fr.all_subgoals_settled():
                break  # Goals 覆盖度驱动收敛：所有子目标已结算（done/exhausted）
            # 复盘会 retro()（设计 §4.1）：空队列时先确定性路由——失败驱动局部递归
            # （§4.2）优先于"规划两轮无产出就收敛"。阶段由状态定：recurse 时编排层
            # 直接产结构化子意图（不经 planner LLM），防"planner 忘了展开"时系统卡死
            if (
                retro(fr, base=base) == "recurse"
                and _expand_recurse(fr, base=base, max_intents=max_intents) > 0
            ):
                continue  # 子意图已入队，worker 认领执行
            # 演化提案（§4.4）：题面外资产集中 → 受控改向通道。ask（默认）进
            # intercept pending 队列等人工审批；auto 只自动批准纯细化类；
            # off 关闭。扩 scope 永远 require 人工（ROE 边界不可自动化）
            _rev_mode = os.environ.get("SECPT_GOAL_REVISION", "ask")
            if _rev_mode != "off":
                _drift = _detect_goal_drift(base, objective)
                if _drift and _queue_goal_revision(
                    fr, base=base, objective=objective, proposal=_drift, mode=_rev_mode
                ):
                    continue  # 提案意图已入队（确定性处置，blocked 等审批）
            if idle_rounds >= 2 or not can_plan:
                break  # 空队列 + 无法再规划（意图数达上限/规划两轮无产出）→ 收敛
            timeout = 2.0  # 空队列短心跳（测试友好；实战 2s 无害）
        else:
            timeout = float(os.environ.get("SECPT_FRONTIER_HEARTBEAT", "60"))
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(wake_q.get(), timeout=timeout)

    stop_ev.set()
    await asyncio.gather(*workers, return_exceptions=True)

    # P0-2：子 job 事件流聚合到父 job（否则父 job 无 events.jsonl，
    # watch 导出 / 评判 evaluate_job 全部失效）。final=True 补漏未增量聚合的
    # 子 job + 追加 summary 事件（增量聚合期间已合并的由 aggregated 幂等跳过）
    _aggregate_sub_events(base, sub_jobs, chains, flags, aggregated=aggregated)

    _release_task_lock(_lock)  # 加强④：正常路径显式释放（异常路径 fd 回收兜底）

    return RunSummary(
        job_id=base,
        mode="frontier",
        flags=flags,
        chains=chains,
        open_remaining=fr.count_open(),
        wall_s=round(time.time() - t0, 1),
        extras={
            "done_events": done_events,  # 完成广播事件（含 report 聚合条目）
            "sub_jobs": sub_jobs,  # 子任务清单（父事件流聚合来源）
        },
    ).to_dict()
