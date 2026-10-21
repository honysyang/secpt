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

_HOST_LIKE_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$|^[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+$")


def _intent_scope_entries(intent) -> list[str] | None:
    """意图授权白名单 = objective 中的实际网络目标 ∪ host 形态的 asset_scope。

    asset_scope 的本职是租约互斥键（常是题号如 c-03），不是网络目标——直接当
    scope 会让引擎拦掉对自己容器的全部请求（tsec1-1036 实证：8 分钟 24 次
    scope.denied，模型被逼写脚本绕过治理链）。objective 里内联的容器地址/
    平台端点才是真目标；纯题号形态（无点号、非 IP）不进白名单。
    """
    entries: list[str] = []
    try:
        from secpt.runtime.engine import extract_scope_targets

        entries.extend(extract_scope_targets(getattr(intent, "objective", "") or ""))
    except Exception:
        pass
    raw = str(getattr(intent, "asset_scope", "") or "").strip()
    if raw and _HOST_LIKE_RE.match(raw) and raw not in entries:
        entries.append(raw)
    return entries or None


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
    max_wall_s INTEGER DEFAULT 0,      -- 意图级墙钟预算（0=不限；长任务/0day 单面时间盒）
    subgoal TEXT DEFAULT '',            -- Goals 层：本意图服务的子目标（跨轮方向一致性的锚点）
    role TEXT DEFAULT '',               -- 角色体系：recon/exploit/verify/report（空=普通意图）
    asset_scope TEXT DEFAULT '',        -- 角色意图挂靠的资产（去重键 + claim 过滤键）
    lane TEXT DEFAULT 'main',           -- 执行通道：main（主路径）| logistics（后台后勤）
    hypothesis TEXT DEFAULT '',         -- 挂靠假设（v4 假设树节点 id；空=无信念挂钩）
    notes TEXT DEFAULT '',              -- 指挥官派单时的注意事项（边界/风险/禁止动作）
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
CREATE TABLE IF NOT EXISTS asset_state (
    asset_scope TEXT PRIMARY KEY,      -- 同资产跨意图交接（M1，通用：recon→exploit→verify）
    last_job_id TEXT DEFAULT '',       -- 上一个产出工作区（新意图启动时搬运产物）
    last_status TEXT DEFAULT '',       -- done/exhausted/blocked
    summary TEXT DEFAULT '',           -- 确定性结论摘要（供下一意图 objective）
    updated_at REAL NOT NULL
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
    max_wall_s: int = 0  # 意图级墙钟预算（秒；0=不限）
    role: str = ""
    asset_scope: str = ""
    tools: str = ""  # 意图级工具覆盖（逗号分隔插件名；空=profile 默认）
    lane: str = "main"  # 执行通道：main 主路径 / logistics 后台后勤
    hypothesis: str = ""  # 挂靠假设 id（v4 假设树；空=无信念挂钩）
    notes: str = ""  # 指挥官派单时的注意事项（边界/风险/禁止动作）
    fail_reason: str = ""


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
            if (
                p.is_file()
                and p.name not in ("api.py", "transcript.json", ".progress.md")
                and p.stat().st_size > 0
            ):
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
    # v4.1：清单持久化（manifest.json）——搬运不再是黑盒，复验/审计可核对
    if lines:
        with contextlib.suppress(Exception):
            import json as _json

            (new_ws / "manifest.json").write_text(
                _json.dumps(
                    {"carried_from": old_job, "files": lines}, ensure_ascii=False, indent=2
                ),
                encoding="utf-8",
            )
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


def _pick_intent_model(role: str, default: str | None, attempts: int = 1) -> str | None:
    """意图级模型路由（通用，非跑分专属）：

    - verify 角色 → SECPT_VERIFY_MODEL（异构验证模型，防同模型同盲区）；
    - attempts>1（首轮失败重试）→ SECPT_ESCALATE_MODEL（0day/硬目标升级强模型）；
    - 其余 → 主模型。
    """
    if role == "verify":
        v = os.environ.get("SECPT_VERIFY_MODEL", "").strip()
        if v:
            return v
    if attempts > 1:
        esc = os.environ.get("SECPT_ESCALATE_MODEL", "").strip()
        if esc:
            return esc
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


def _parent_event(base: str, etype: str, payload: dict) -> None:
    """编排层事件直写父流（对话面板的轨迹源）。

    seq 取当前行数 +1（与 _aggregate_sub_events 同一口径，单调不回退）；
    全程同步无 await，与主循环聚合写入互不交错。best-effort：观测层失败不炸编排。
    """
    import json as _json

    from secpt.core.job.resume import jobs_dir

    try:
        parent = jobs_dir() / base
        parent.mkdir(parents=True, exist_ok=True)
        f = parent / "events.jsonl"
        seq = 0
        if f.is_file():
            with open(f, encoding="utf-8") as rf:
                seq = sum(1 for _ in rf)
        with open(f, "a", encoding="utf-8") as out:
            out.write(
                _json.dumps(
                    {
                        "seq": seq + 1,
                        "job_id": base,
                        "ts": time.time(),
                        "round": 0,
                        "type": etype,
                        "payload": payload,
                        "actor": {
                            "plan_event": "commander",
                            "intent_event": "sentinel",
                        }.get(etype, "sentinel"),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass


def _write_retro(
    *,
    base: str,
    objective: str,
    flags: list[str],
    chains: list,
    delivery,
    fr: Frontier,
    wall_s: float = 0.0,
) -> Path | None:
    """主智能体收尾：把战果、任务链、否定点与交付判定写成 retro.md。

    确定性、零 LLM；与 solve 的经验蒸馏互补（蒸馏把成功路径写入经验库，
    retro 留下本次运行的完整审计摘要）。任何失败都静默降级，不影响主返回。
    """
    try:
        from secpt.core.job.resume import jobs_dir

        path = jobs_dir() / base / "retro.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        done = [c for c in chains if str(getattr(c, "status", "")) == "done"]
        failed = [
            c for c in chains if str(getattr(c, "status", "")) in ("exhausted", "blocked", "failed")
        ]
        lines = [
            "# 任务复盘（主智能体自动生成）",
            "",
            f"- 任务：{base}",
            f"- 目标：{objective[:1200]}",
            f"- 用时：{wall_s}s",
            f"- 战果：{', '.join(flags) if flags else '（无 flag）'}",
            f"- 任务链：{len(chains)}（done {len(done)} / 异常 {len(failed)}）",
            f"- 主路径剩余：{fr.count_open(lane='main')}；后勤剩余：{fr.count_open(lane='logistics')}",
        ]
        if delivery is not None:
            as_dict = delivery.as_dict() if hasattr(delivery, "as_dict") else delivery
            lines.append(
                f"- 交付判定：{'通过' if as_dict.get('accepted') else '未通过'}"
                f" — {as_dict.get('reason', '')}"
            )
        else:
            lines.append("- 交付判定：未运行（截止/轮次耗尽）")
        lines += ["", "## 任务链", ""]
        for c in chains:
            lines.append(
                f"- [{getattr(c, 'status', '')}] {getattr(c, 'intent', '')} "
                f"(source={getattr(c, 'source', '')}, steps={getattr(c, 'steps', 0)}, "
                f"lane={getattr(c, 'lane', '-')})"
            )
        if failed:
            lines += ["", "## 失败/否定（下一轮先看这里）", ""]
            for c in failed[:12]:
                lines.append(
                    f"- [{getattr(c, 'status', '')}] {getattr(c, 'intent', '')}: "
                    f"{str(getattr(c, 'fail_reason', '') or '')[:240]}"
                )
        lines += [
            "",
            "## 沉淀",
            "",
            "- 成功路径/适用条件由 solve 的经验蒸馏进入经验库；",
            "- 本文件是本次运行的审计摘要，供下一轮 planner 与人工复盘使用。",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    except Exception:
        return None


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
            with contextlib.suppress(sqlite3.Error):
                c.execute("PRAGMA journal_mode=WAL")  # 持久化，无需每连接重设
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
        if "max_wall_s" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN max_wall_s INTEGER DEFAULT 0")
        if "subgoal" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN subgoal TEXT DEFAULT ''")
        if "role" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN role TEXT DEFAULT ''")
        if "asset_scope" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN asset_scope TEXT DEFAULT ''")
        if "tools" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN tools TEXT DEFAULT ''")
        if "lane" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN lane TEXT DEFAULT 'main'")
        if "hypothesis" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN hypothesis TEXT DEFAULT ''")
        if "notes" not in cols:
            with contextlib.suppress(sqlite3.Error):
                c.execute("ALTER TABLE intents ADD COLUMN notes TEXT DEFAULT ''")

    def _conn(self) -> sqlite3.Connection:
        # 多 worker 线程会并发认领/结算；给足锁等待（默认 5s 太短：一次长写事务
        # 就会让认领线程抛 database is locked，进而让 worker 协程死亡——
        # 见 run_frontier 的 worker 监管）。WAL 由 __init__ 持久化设置。
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.execute("PRAGMA busy_timeout=15000")
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
        max_wall_s: int = 0,  # 意图级墙钟预算（秒；0=不限，长任务单面时间盒）
        role: str = "",  # 角色体系：recon/exploit/verify/report（空=普通意图）
        asset_scope: str = "",  # 挂靠资产（去重键 + claim 过滤键）
        tools: str = "",  # 意图级工具覆盖（逗号分隔插件名；空=profile 默认）
        lane: str = "main",  # 执行通道：main 主路径 / logistics 后台后勤
        hypothesis: str = "",  # 挂靠假设（v4 假设树节点 id）
        notes: str = "",  # 指挥官派单时的注意事项（边界/风险/禁止动作）
    ) -> str:
        iid = f"int-{uuid.uuid4().hex[:8]}"
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT INTO intents (id,title,goal,done_criteria,priority,depth,status,
                                        source,depends_on,subgoal,max_steps,max_wall_s,
                                        role,asset_scope,lane,hypothesis,notes,tools,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    iid,
                    title[:200],
                    goal[:2000],
                    done_criteria[:500],
                    int(priority),
                    int(depth),
                    source,
                    depends_on[:300],
                    subgoal[:300],
                    int(max_steps),
                    int(max_wall_s),
                    role[:20],
                    asset_scope[:300],
                    lane[:20],
                    hypothesis[:40],
                    notes[:500],
                    tools[:300],
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

    def count_active_by_asset(self, asset_scope: str) -> int:
        """同一资产上仍处 open/running 的意图数（A1 分段结算：全段结束才关容器）。"""
        if not asset_scope:
            return 0
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) n FROM intents WHERE asset_scope=?"
                " AND status IN ('open','running')",
                (asset_scope,),
            ).fetchone()
        return int(row["n"] or 0)

    def finish_open_by_asset(self, asset_scope: str, *, result: str = "") -> int:
        """平台已通关：把同资产未开始的段直接落 done（跳过剩余段，省步数）。"""
        if not asset_scope:
            return 0
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                "UPDATE intents SET status='done', result=?, updated_at=?"
                " WHERE asset_scope=? AND status IN ('open','blocked')",
                (result[:500], now, asset_scope),
            )
            return int(cur.rowcount or 0)

    def record_asset_state(
        self, asset_scope: str, *, job_id: str, status: str, summary: str
    ) -> None:
        """M1：记录同资产最近一次执行的工作区与结论（跨意图交接的持久锚点）。"""
        if not asset_scope:
            return
        with contextlib.suppress(sqlite3.Error), self._conn() as c:
            c.execute(
                "INSERT INTO asset_state (asset_scope,last_job_id,last_status,summary,updated_at)"
                " VALUES (?,?,?,?,?) ON CONFLICT(asset_scope) DO UPDATE SET"
                " last_job_id=excluded.last_job_id, last_status=excluded.last_status,"
                " summary=excluded.summary, updated_at=excluded.updated_at",
                (asset_scope[:300], (job_id or "")[:120], status[:20], summary[:800], time.time()),
            )

    def asset_state(self, asset_scope: str) -> dict | None:
        """M1：取同资产上一意图的状态（无则 None）。"""
        if not asset_scope:
            return None
        with contextlib.suppress(sqlite3.Error), self._conn() as c:
            row = c.execute(
                "SELECT * FROM asset_state WHERE asset_scope=?", (asset_scope[:300],)
            ).fetchone()
            if row is not None:
                return dict(row)
        return None

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

    def claim_next(self, role: str | None = None, lane: str | None = None) -> Intent | None:
        """领下一条：priority DESC → depth DESC（深度优先）→ 先创建先领。

        role 指定时只认领该角色（或未定角色的老意图）——角色 worker 拓扑：
        声明确认自己角色后，认领互斥由原子 UPDATE 保证，不抢其他角色的活。
        lane 指定时只认领该通道（main 主路径 / logistics 后台后勤）。
        跨进程原子性：BEGIN IMMEDIATE 先取写锁再做 SELECT+UPDATE——deferred
        事务下 SELECT 不加写锁，两个进程可能同时选中同一行再各自 UPDATE
        成功（双认领、attempts 双计）；持写锁后该序列不可交错。
        """
        role_filter = "AND (role=? OR role='')" if role else ""
        lane_filter = "AND lane=?" if lane else ""
        args: list[str] = []
        if role:
            args.append(role)
        if lane:
            args.append(lane)
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            # 加强①：资产租约——带 asset_scope 的意图在**同一事务内**抢占租约，
            # 他人持有的资产一律跳过。此前同资产意图可被两个 worker 并发执行
            # （只有 SharedStopper 的事后让出），登录态/有状态靶场会互扰。
            row = c.execute(
                f"""SELECT * FROM intents WHERE status='open' {role_filter} {lane_filter}
                   AND (asset_scope='' OR asset_scope NOT IN
                        (SELECT asset_scope FROM asset_leases))
                   ORDER BY priority DESC, depth DESC, created_at LIMIT 1""",
                tuple(args),
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
        return self._intent_from_row(row, int(row["attempts"]) + 1)

    @staticmethod
    def _intent_from_row(row: sqlite3.Row, attempts: int) -> Intent:
        d = dict(row)  # sqlite3.Row 的 `in` 按值迭代不按键——必须 dict 化后 get（老库缺列兼容）
        return Intent(
            id=row["id"],
            title=row["title"],
            goal=row["goal"],
            done_criteria=row["done_criteria"],
            priority=row["priority"],
            depth=row["depth"],
            status="running",
            attempts=attempts,
            source=d.get("source", "planner"),
            depends_on=d.get("depends_on", "") or "",
            max_steps=int(d.get("max_steps", 0) or 0),
            max_wall_s=int(d.get("max_wall_s", 0) or 0),
            role=d.get("role", "") or "",
            asset_scope=d.get("asset_scope", "") or "",
            tools=d.get("tools", "") or "",
            lane=d.get("lane", "main") or "main",
            hypothesis=d.get("hypothesis", "") or "",
            notes=d.get("notes", "") or "",
            fail_reason=d.get("fail_reason", "") or "",
        )

    def claim_by_id(self, iid: str) -> Intent | None:
        """按 id 认领指定意图（指挥官自执行路径：派单后立即自取，不抢队列）。

        与 claim_next 同一事务语义（BEGIN IMMEDIATE + 租约），仅 open 可领。
        """
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM intents WHERE id=? AND status='open'", (iid,)
            ).fetchone()
            if row is None:
                return None
            c.execute(
                "UPDATE intents SET status='running', attempts=attempts+1, updated_at=?"
                " WHERE id=? AND status='open'",
                (time.time(), iid),
            )
            _scope = str(row["asset_scope"] or "")
            if _scope:
                c.execute(
                    "INSERT OR REPLACE INTO asset_leases (asset_scope, intent_id, acquired_at)"
                    " VALUES (?,?,?)",
                    (_scope, iid, time.time()),
                )
        return self._intent_from_row(row, int(row["attempts"]) + 1)

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

    def count_open(self, *, lane: str | None = None) -> int:
        sql = "SELECT COUNT(*) n FROM intents WHERE status='open'"
        args: tuple[str, ...] = ()
        if lane:
            sql += " AND lane=?"
            args = (lane,)
        with self._conn() as c:
            return int(c.execute(sql, args).fetchone()["n"])

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

    def open_summary(self, limit: int = 10, *, lane: str | None = "main") -> list[dict]:
        sql = "SELECT id,title,priority,depth,lane FROM intents WHERE status='open'"
        args: list = []
        if lane:
            sql += " AND lane=?"
            args.append(lane)
        sql += " ORDER BY priority DESC, depth DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            rows = c.execute(sql, args).fetchall()
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
6. 角色挂靠（必须）：每条意图带 "role"（recon/exploit/lateral/verify/report）与
   "asset_scope"——asset_scope 必须取自【已有资产实体】；不在实体集内的意图作废（不派发）
7. 去重：同 asset_scope+role 的意图已存在（见【当前待办】）就不要再派
8. 依赖声明：需要前置成果才能启动的意图带 "depends_on"——两种取值：
   - intent_id（【当前待办】里的 id）：前置意图 done 后才解锁；
   - fact:谓词（如 fact:credential、fact:access）：图记忆出现匹配事实即解锁
     （凭据/访问权类横向意图一律用 fact:credential 声明，等 exploit record_fact 后自动解锁）

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
"role": "recon|exploit|lateral|verify|report", "asset_scope": "已有资产实体中的一项",
"depends_on": "intent_id 或 fact:谓词，可省略"}}]"""


# Default v3 planner: generic, independent tasks; no role templates.
# (Legacy role planner above remains available via SECPT_ROLE_TEMPLATES=1.)
_PLANNER_PROMPT_V3 = """你是渗透任务的主智能体（Planner）。根据【总目标】【近期结果】【当前待办】规划下一步，只产出彼此独立、可由任意执行体领取的任务。

纪律（必须遵守）：
1. 任务独立：任务之间默认没有依赖，不要输出 depends_on；执行体领取后独立完成。
2. 不预设角色：不要输出 role，也不要把任务写成 recon/exploit/lateral 角色模板；
   任务描述本身说明要做什么、完成判据是什么即可。
3. 深度优先：优先派能推进当前最有希望利用链的任务，不要浅层铺开。
4. 否定记账：近期结果里已证伪的路线不要重复派（至多复核一次）。
5. 每条任务必须可判定：done_criteria 写清"怎样算做完"。
6. 最多产 {max_new} 条；没有值得做的就产空列表（任务自然收敛）。
7. 子目标记账：可用可选字段 "subgoal"；优先覆盖【子目标覆盖度】里未测的方向。
8. 后勤分流：下载工具/依赖、大批量搜索、字典/爆破、爬取、长耗时收集这类
   不阻塞主路径的工作，标 "lane": "logistics"；其余标 "lane": "main"。
   需要验证 flag/结论、复现、交付的任务一律 main，不要放后勤。
9. 资产范围：可选 "asset_scope"，若填写必须取自【已有资产实体】；没有明确资产就留空。

【总目标】
{objective}

【近期结果（含后台后勤结果，可合并使用）】
{results}

【当前待办（main 通道）】
{opens}

【图记忆·最新事实/发现（跨任务）】
{facts}

【覆盖度缺口·未触达资产（优先派这里）】
{untested}

【已有资产实体（asset_scope 可选，必须从这里选）】
{entities}

【子目标覆盖度】
{coverage}

只输出 JSON：[{{"title": "...", "goal": "...", "done_criteria": "...",
"priority": 1-10, "depth": {next_depth}, "subgoal": "...",
"lane": "main|logistics", "asset_scope": "可选，已有资产实体中的一项"}}]"""


async def plan_round(
    objective: str,
    frontier: Frontier,
    llm,
    *,
    model: str = "deepseek-chat",
    max_new: int = 3,
    next_depth: int = 0,
) -> int:
    """planner 新会话产意图。解析失败/无 llm 返回 0（调度继续，不阻塞）。

    默认 v3：任务彼此独立、无角色模板；triage 快判决定 main/logistics 通道。
    ``SECPT_ROLE_TEMPLATES=1`` 恢复旧的角色 planner（兼容测试/旧部署）。
    """
    from secpt.core.orchestrator.triage import (
        TaskTriage,
        refine_with_jev,
        triage_task,
    )

    legacy_roles = os.environ.get("SECPT_ROLE_TEMPLATES", "0") == "1"
    situation = frontier.situation_report()
    prompt_template = _PLANNER_PROMPT if legacy_roles else _PLANNER_PROMPT_V3
    prompt = prompt_template.format(
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
    # 资产挂靠强制：实体集（含 goal-seed 种子），不在集内的资产直接丢弃
    known = {a["value"] for a in situation.get("known_assets", [])}
    for item in data[:max_new]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        role = str(item.get("role") or "").strip() if legacy_roles else ""
        asset_scope = str(item.get("asset_scope") or "").strip()
        # 空想意图防线：声明资产的意图必须挂靠已知实体，否则丢弃（不派发）
        if asset_scope and asset_scope not in known:
            continue
        # 去重：仅旧角色模式按 asset_scope+role 去重（通用任务允许不同角度并行）
        if role and asset_scope and frontier.find_duplicate(role, asset_scope):
            continue
        goal = str(item.get("goal") or "")
        done_criteria = str(item.get("done_criteria") or "")
        try:
            requested_priority = int(item.get("priority", 5))
        except (TypeError, ValueError):
            requested_priority = 5
        triage = triage_task(
            title,
            goal,
            done_criteria,
            priority=requested_priority,
            role=role,
            asset_scope=asset_scope,
        )
        if str(item.get("lane") or "").strip() == "logistics" and not triage.needs_delivery:
            triage = TaskTriage(
                lane="logistics",
                priority=triage.priority,
                needs_delivery=triage.needs_delivery,
                risk=triage.risk,
                reason=(triage.reason + "；" if triage.reason else "") + "planner 标记为后勤",
            )
        triage = await refine_with_jev(triage, title=title, goal=goal)
        subgoal = str(item.get("subgoal") or "").strip()
        frontier.add(
            title,
            goal=goal,
            done_criteria=done_criteria,
            priority=int(triage.priority or requested_priority),
            depth=int(item.get("depth", next_depth)),
            subgoal=subgoal,
            role=role,
            asset_scope=asset_scope,
            depends_on=str(item.get("depends_on") or "") if legacy_roles else "",
            lane=triage.lane,
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
    global_objective: str,
    intent: Intent,
    recent: list[dict],
    *,
    base: str = "",
    handoff: str = "",
    allow_logistics: bool = False,
) -> str:
    recent_txt = "\n".join(
        f"- [{r['status']}] {r['title']}: {r['result'][:200]}" for r in recent[:4]
    )
    # 角色体系（legacy）：仅 SECPT_ROLE_TEMPLATES=1 时注入角色化阶段提示。
    # v3 默认不使用角色模板——任务本身就是完整语义，执行体是通用 ReAct agent。
    role_stage = ""
    if os.environ.get("SECPT_ROLE_TEMPLATES", "0") == "1" and getattr(intent, "role", ""):
        from secpt.core.orchestrator.roles import role_stage as _rs

        role_stage = "\n\n" + _rs(intent.role, getattr(intent, "asset_scope", "") or "")
    # M4-c：通用打法模板（数据驱动、按信号命中；只注入线索，不做执行）
    techniques = ""
    try:
        from secpt.learning.techniques import match_techniques

        _tech_text = match_techniques(
            " ".join([intent.title, intent.goal, getattr(intent, "asset_scope", "") or ""])
        )
        if _tech_text:
            techniques = "\n\n" + _tech_text
    except Exception:
        techniques = ""
    # 跨资产情报：注入本任务已确认漏洞（P2 情报共享）——同目标不同
    # 资产的漏洞模式/凭据路径通常可复用，避免各执行体重复挖同一类洞。
    # v3 通用执行体没有角色，因此不再用 role 过滤。
    shared = ""
    if base:
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
    logistics = ""
    if allow_logistics:
        logistics = (
            "\n\n【后勤委托】遇到下载工具/依赖、大批量搜索、字典/爆破、爬取、长耗时收集"
            "等会拖慢主路径的工作，先调用 delegate_logistics 异步交给后勤，再立即继续"
            "当前任务；不要为这类工作阻塞等待。验证 flag/结论、复现与交付不要交给后勤。"
        )
    # v4.1 智能体层：挂靠假设的上下文注入——执行体不是领"一段文字"，是在验证一个
    # 可证伪假设；实验设计必须围绕判定实验（test），结论按假设语义落点。
    hypothesis_ctx = ""
    _hid = getattr(intent, "hypothesis", "") or ""
    if _hid and base:
        try:
            from secpt.core.orchestrator.hypothesis import (
                HypothesisTree,
                hypothesis_db_path,
            )

            _node = HypothesisTree(hypothesis_db_path(base)).get(_hid)
            if _node is not None:
                hypothesis_ctx = (
                    f"\n\n【你在验证的假设（{_hid}，置信度 {_node.confidence:.0%}）】"
                    f"\n- 假设：{_node.statement}"
                    f"\n- 判定实验：{_node.test}"
                    f"\n- 已有证据：{('、'.join(_node.evidence_refs[-3:])) or '（无）'}"
                    "\n本任务的结果会直接更新该假设（证实→confirmed / 证伪→refuted），"
                    "结论请明确回答判定实验的落点。"
                )
        except Exception:
            hypothesis_ctx = ""
    # v4.1 协作层：后勤已完成成果对主任务可见（否则后勤干了活主任务不知道）
    logistics_done = ""
    _done_logi = [
        r for r in recent if r.get("status") == "done" and "后勤" in str(r.get("title", ""))
    ]
    if _done_logi:
        _lines = ["", "【后勤已完成（直接取用，不要重复准备）】"]
        for r in _done_logi[:3]:
            _lines.append(f"- {r['title']}: {str(r.get('result', ''))[:160]}")
        logistics_done = "\n".join(_lines)
    # 指挥官派单元数据：截止（意图级墙钟预算）与注意事项（边界/风险/禁止动作）——
    # 与 GoalSpec 的操作约束互补：约束是任务级，notes 是单意图级的临场纪律
    dispatch_meta = ""
    _notes = (getattr(intent, "notes", "") or "").strip()
    if _notes:
        dispatch_meta += f"\n注意事项：{_notes[:400]}"
    _mws = int(getattr(intent, "max_wall_s", 0) or 0)
    if _mws > 0:
        dispatch_meta += f"\n时限：本任务预计 {_mws} 秒内完成（到点即结算，不要恋战）"
    return f"""【总目标】
{global_objective}

【当前意图】{intent.title}
目标：{intent.goal}
完成判据：{intent.done_criteria or "拿到该意图的结论即可"}{role_stage}{hypothesis_ctx}{dispatch_meta}
{handoff}{techniques}

【近期相关结果（避免重复踩坑）】
{recent_txt or "（无）"}{shared}{logistics}{logistics_done}

要求：只推进当前意图，完成判据满足后以 ANSWER: <结论> 收尾。
结论必须证据锚定：关键证据给出文件路径/命令输出摘录/哈希之一，没有证据支撑的断言不要写。"""


_DELEGATE_LOGISTICS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_logistics",
        "description": (
            "把下载工具/依赖、大批量搜索、字典/爆破、爬取、长耗时收集等耗时且重复的工作"
            "异步交给后勤智能体，立即返回，不阻塞当前任务。结果稍后写入任务池/工作台。"
            "不要用它验证 flag/结论、复现或交付。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "完整、可独立执行的后勤任务（要做什么、目标、期望产物）",
                },
                "why": {"type": "string", "description": "为什么适合后台执行（可选）"},
            },
            "required": ["task"],
        },
    },
}


def _make_delegate_logistics_tool(fr: Frontier, base: str, parent: Intent) -> dict:
    """Build the per-intent ``delegate_logistics`` closure injected into the engine.

    The tool is synchronous and enqueues directly into the task pool.  It uses a
    fresh SQLite connection per call (Frontier methods do), so it is safe from the
    engine's worker thread.  Logistics tasks deliberately carry no asset lease:
    they must not block the main task on the same asset.
    """

    def _fn(args: dict) -> str:
        task = str((args or {}).get("task") or "").strip()
        if not task:
            return "Error: 缺少 task（要交给后勤的具体工作）"
        try:
            if fr.count_open(lane="logistics") >= 20:
                return "Error: 后勤队列已满（20 条），先等待现有任务回写"
            why = str((args or {}).get("why") or "").strip()
            goal = f"【父任务】{parent.title}\n任务：{task}"
            if why:
                goal += f"\n为什么适合后台：{why}"
            if getattr(parent, "asset_scope", ""):
                goal += f"\n父任务资产：{parent.asset_scope}"
            iid = fr.add(
                f"后勤：{task[:60]}",
                goal=goal,
                done_criteria="产出结果并回写任务池/工作台；不阻塞主路径",
                priority=2,
                depth=0,
                source="worker",
                lane="logistics",
                max_steps=30,
                max_wall_s=300,
            )
            return (
                f"已交给后勤（{iid}），后台执行。不要等待结果；继续当前任务的下一步，"
                "后勤完成后会把结果写入任务池/工作台。"
            )
        except Exception as e:
            return f"Error: 后勤委托失败: {e}"

    return {"delegate_logistics": (_DELEGATE_LOGISTICS_SCHEMA, _fn)}


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
    stages: object | None = None,  # S6 模式收敛：profile.stages 折叠为线性 depends_on 意图链
    # （原 run_stages 多阶段编排的唯一存续形态；调用方以 max_intents=len(stages) 关闭 planner）
    default_max_steps: int = 0,  # S6：意图未设步数预算时的全局默认（solve --steps 透传；0=profile 预算）
    on_started: object | None = None,  # P0-2：意图「真正开跑」回调 intent->None（如置 running）
    on_settled: object | None = None,  # ② 结算回调 intent->None：编排层确定性收尾（如 close 容器）
    scope_ledger: object | None = None,  # ③ 跨进程广播载体（ChallengeLedger，缺省懒建同库实例）
    should_drain: object | None = None,  # A3：优雅排空回调 ()->bool；True 时停止认领新意图
    deadline_ts: float | None = None,  # 通用全局截止（epoch 秒）；缺省读 SECPT_DEADLINE_TS
    reserve_s: float = 300.0,  # 截止前预留秒数（留给收尾/报告，不再开新意图）
    enable_logistics: bool = False,  # 后勤后台通道（dag/solve 主入口开启；直调默认关）
    enable_delivery: bool = False,  # 交付门（dag/solve 主入口开启；直调默认关）
    delivery_max_attempts: int = 1,  # 交付门最多补几次验证任务（防无限追加）
    enable_commander: bool = True,  # v4 Commander（假设树+战略文档+事件唤醒；benchmark/stages 强制关）
    scope_allow: list[str] | None = None,  # goal asset_scope 白名单（entries+SECPT_SCOPE_ALLOW；空=跳过校验）
) -> dict:
    """意图驱动主循环。返回汇总（chains/flags/wall）。

    模式语义：
    - 队列中存在未完成的 direct（人工直投）意图 → 规划者暂停，只执行 direct，
      全部 direct 完成且无新 direct → 结束（ARTEX 模式②）；此后恢复正常规划
    - D2.2：max_workers>1 时并发跑多个 claim→run→settle 循环（原子认领）
    - v3：planner 默认产独立通用任务（无角色模板），triage 快判 main/logistics；
      后勤在独立通道后台执行，不阻塞主路径。主路径结算后由交付门核验战果/
      复验并决定结束；benchmark 平台协议关闭后勤与交付门，保持跑分语义。
    - role_plan=True（legacy，仅 SECPT_ROLE_TEMPLATES=1）：旧角色骨架仍可用。
    """
    import asyncio

    from secpt.cli.engine_runner import resolve_models, run_engine

    # v4.2 去协议降级：benchmark 也走完整智能层（Commander/后勤/交付门）——
    # 架构不应按任务类型打折；跑分协议纪律由 benchmark 先验（objective 铁律）
    # 与台账对账保证，而非关闭回路。env 开关仍可按需关闭。
    if stages:
        enable_commander = False  # 清单型链路保持逐段执行语义

    model, _fb = resolve_models(model, None)

    # llm 缺省时解析生产 LLM——planner 与 worker 共用（懒升级路径此前传 None 导致
    # planner 静默死亡：plan_round 捕获 AttributeError 返回 0 意图）
    if llm is None or planner_llm is None:
        from secpt.cli.engine_runner import _prod_llm

        _prod = _prod_llm()
        llm = llm or _prod
        planner_llm = planner_llm or _prod

    # 目标理解归指挥官（首轮唤醒产 goal）。三个来源按序：
    # ① resume：goal.json 已落盘 → 直接渲染续用；② legacy 角色骨架 / stages 链：
    # 骨架要先于指挥官搭好，入口理解兜底；③ 其余：标记 need_goal，首轮唤醒交指挥官。
    # benchmark 是程序化协议，不拆解；SECPT_GOALS=0 关闭。
    from secpt.core.orchestrator.goals import (
        decompose_objective,
        filter_goal_assets,
        is_rendered,
        load_goal,
        render_goal_system,
        seed_goal_assets,
    )

    # F1：base 先定（意图库按任务隔离），库即 var/jobs/<base>/frontier.sqlite——
    # 并发任务互不可见；resume 同 base 续接同一 todolist
    base = base_job_id or f"hunt-{uuid.uuid4().hex[:8]}"
    # 加强④：同任务单编排锁——第二个并发实例直接拒绝（防双方 _recover_stale
    # 互重置在途意图）。异常路径由文件对象回收自动释放 flock（CPython 语义）
    _lock = _acquire_task_lock(base)
    fr = frontier or Frontier(frontier_db_path(base))
    # v4：Commander 双载体的第二载体——假设树（benchmark/stages 清单型不建）
    tree = None
    watch = None
    if enable_commander:
        try:
            from secpt.core.orchestrator.hypothesis import HypothesisTree, hypothesis_db_path
            from secpt.core.orchestrator.watch import Watch

            tree = HypothesisTree(hypothesis_db_path(base))
            watch = Watch(max_intents=max_intents)
            from secpt.core.orchestrator.commander import strategy_path

            _strategy_path = strategy_path(base)
        except Exception as _e:  # 假设树故障不阻断任务（降级 v3 planner）
            print(f"[frontier] 假设树初始化失败，降级 v3 planner: {_e}", flush=True)
            tree = None
            watch = None

    # 目标理解三来源（见上方注释）：resume 落盘复用 > legacy/stages 入口兜底 > 指挥官首轮
    goal = None
    need_goal_pending = False
    if (
        profile != "benchmark"
        and not is_rendered(objective)
        and os.environ.get("SECPT_GOALS", "1") != "0"
    ):
        _g = load_goal(base)
        if _g is not None:
            goal = _g
            objective = render_goal_system(goal)
            print("[commander] 目标理解：沿用已落盘 goal.json（resume 续接）", flush=True)
        elif role_plan or stages:
            goal = await decompose_objective(objective, llm=llm, model=model)
            seed_goal_assets(goal)
            objective = render_goal_system(goal)
        elif enable_commander and tree is not None:
            need_goal_pending = True  # 指挥官首轮唤醒完成理解+拆分
            print("[commander] 目标理解：待指挥官首轮唤醒完成", flush=True)

    # 团队模式（role_plan）：折叠 Specialist 固定 DAG 为角色意图骨架——
    # recon 1 条（role=recon）+ 每资产 1 条 exploit（role=exploit, depends_on=recon）
    # + lateral 1 条（role=lateral, depends_on=fact:credential——exploit/recon
    # record_fact 出凭据事实即解锁，deps_satisfied 确定性判定）。
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
            if not fr.find_duplicate("lateral", ",".join(team_assets)[:300]):
                fr.add(
                    "横向移动（lateral）",
                    goal="复用已获凭据/访问权做横向扩散（内网服务、相邻主机、数据定位）",
                    done_criteria="凭据复用验证完成，产出可达范围结论",
                    priority=6,
                    depth=2,
                    depends_on="fact:credential",  # intel 图出现 credential:* 事实即解锁
                    role="lateral",
                    asset_scope=",".join(team_assets)[:300],
                )
            max_workers = max(max_workers, min(len(team_assets), 4))  # exploit 并行认领
            stop_on_first_flag = False  # 团队模式：持续推进所有资产，不因单资产收敛

    # S6 模式收敛——多阶段折叠：profile.stages → 线性 depends_on 意图链。
    # 原 run_stages 编排器已退役（policy.DEPRECATED_MODULES），stages 是唯一执行
    # 编排 frontier 的一种清单型调用：阶段间交接复用 M1 同资产 asset_state 搬运
    # （全链共用同一 stage_key 作 asset_scope）；阶段预算/工具/收尾条件映射意图
    # 字段；planner 由调用方以 max_intents=len(stages) 关闭（同 benchmark 语义）。
    if stages:
        stage_key = f"stages:{profile or 'task'}"
        prev_id = ""
        for _idx, _st in enumerate(stages):
            _goal = (_st.role or "").strip() or f"执行阶段 {_st.name}"
            if getattr(_st, "prior", ""):
                _goal = f"【阶段先验】{_st.prior}\n{_goal}"
            _iid = fr.add(
                f"阶段 {_idx + 1}/{len(stages)}：{_st.name}",
                goal=_goal,
                done_criteria=(_st.stop_condition or "").strip()[:500],
                priority=9 - _idx,  # 保证段序（同优先级下认领顺序不保证）
                depth=_idx,
                depends_on=prev_id,
                role=_st.name[:20],
                asset_scope=stage_key,
                tools=",".join(_st.tools or []),
                max_steps=int(_st.budget.steps or 0),  # 0 → default_max_steps/profile
                max_wall_s=int(_st.budget.wall_s or 0),
            )
            prev_id = _iid
        max_workers = 1  # depends_on 链已串行，多 worker 只会空等租约
        stop_on_first_flag = False  # 阶段语义：全链跑完（中间阶段 ANSWER 是交付物）

    chains: list[dict] = []
    flags: list[str] = []
    chains_lock = asyncio.Lock()
    stopper = SharedStopper(scope_ledger or _get_scope_ledger())  # 完成广播（进程内+账本跨进程）
    done_events: list[dict] = []  # 完成事件收集（settle done，供调用方聚合）
    sub_jobs: list[str] = []  # 子任务 job_id（父 job 事件流聚合用，P0-2）
    aggregated: set[str] = set()  # ④ 已增量聚合的子 job（幂等去重）
    idle_rounds = 0
    # v4 值守层：待消费唤醒信号（FIFO，Commander 每次取一条）；tree/watch 在下方初始化
    pending_signals: list = []
    _self_exec_pending: list[str] = []  # 指挥官自执行任务的意图 id（派单后由主循环内联跑）
    commander_decisions: list = []
    stall_wake_budget = 2  # 停滞触发的额外唤醒上限（防无信念增量的无限重规划）
    max_new_per_wake = 3  # Commander 每次唤醒派单上限
    _strategy_path = None  # 战略文档路径（树建好后落位）
    delivery_pending = False  # 有候选战果/队列将尽，等待交付门
    delivery_wait = False  # 已派发交付复验任务，暂停规划直到复验结算
    delivery_attempts = 0  # 已派发的交付 follow-up 次数
    delivery_decision = None  # 最终交付判定（进 summary/retro）
    t0 = time.time()
    # 事件唤醒队列：外部传入则共用（D2.3 外部触发），否则本地队列——
    # worker 结算/心跳都会 put，主循环据此立即重估（不等 60s 心跳饿死）
    wake_q: asyncio.Queue = wake_queue if wake_queue is not None else asyncio.Queue()

    def _stall_signal_now(detail: str):
        from secpt.core.orchestrator.watch import WakeSignal

        return WakeSignal(kind="stall", detail=detail, priority=6)

    def _prune_dead_tasks() -> int:
        """内务修剪：挂靠假设已 dead/refuted 的未开始任务直接取消（防死节点残留）。"""
        if tree is None:
            return 0
        n = 0
        try:
            for row in fr.list_all(status="open", limit=200):
                hid = str(row.get("hypothesis") or "")
                if not hid:
                    continue
                node = tree.get(hid)
                if node is not None and node.state in ("dead", "refuted"):
                    fr.settle(
                        row["id"],
                        status="exhausted",
                        result="",
                        fail_reason=f"挂靠假设 {hid} 已 {node.state}，任务随之取消（值守层修剪）",
                    )
                    n += 1
        except Exception:
            pass
        return n

    async def _maybe_plan() -> int:
        """规划者轮（v4）：值守层信号驱动 Commander；无树/降级时走 v3 planner。

        人工接管（direct 未完成）/等待交付复验时暂停。
        首轮 need_goal：Commander 响应中的 goal → 白名单过滤 → 落盘 goal.json
        → 种子资产入图 → objective 替换为结构化文本（此后所有简报/任务共享）。
        """
        nonlocal idle_rounds, stall_wake_budget, delivery_pending, objective, need_goal_pending
        signal = None  # 本轮唤醒信号（Commander 分支赋值；legacy planner 兜底 None）
        if fr.has_open_direct():
            return 0  # 模式②：规划者暂停
        if delivery_pending or delivery_wait:
            return 0  # 交付门未收口：暂停产新意图，等复验结算
        if fr.total() >= max_intents or idle_rounds >= 2:
            return 0
        with fr._conn() as c:
            row = c.execute("SELECT MAX(depth) d FROM intents").fetchone()
            max_depth = int(row["d"] or 0)
        next_depth = max_depth + (1 if fr.total() else 0)
        if tree is not None and watch is not None:
            from secpt.core.orchestrator.commander import commander_round

            signal = pending_signals.pop(0) if pending_signals else None
            _send_need_goal = need_goal_pending
            need_goal_pending = False  # 只给首轮一次理解机会；失败即降级原文
            outcome = await commander_round(
                objective,
                fr,
                tree,
                planner_llm,
                model=model,
                signal=signal.as_dict() if signal is not None else None,
                path=_strategy_path,
                max_new=max_new_per_wake,
                next_depth=next_depth,
                asset_pool=list(default_assets or []),
                need_goal=_send_need_goal,
            )
            # 首轮任务理解产物：过滤（输入可信边界）→ 落盘 → 种子资产 → 结构化
            if outcome.goal is not None:
                _allow_src = [a for a in (scope_allow or []) if a and a.strip()]
                if _allow_src:
                    outcome.goal.asset_scope = filter_goal_assets(
                        outcome.goal.asset_scope, _allow_src
                    )
                with contextlib.suppress(Exception):
                    seed_goal_assets(outcome.goal)
                with contextlib.suppress(Exception):
                    from secpt.core.orchestrator.goals import save_goal

                    save_goal(base, outcome.goal)
                objective = render_goal_system(outcome.goal)
                print(
                    f"[commander] 任务理解完成：assets={len(outcome.goal.asset_scope)} "
                    f"constraints={len(outcome.goal.constraints)} "
                    f"steps={len(outcome.goal.attack_steps)}（goal.json 已落盘）",
                    flush=True,
                )
            # 指挥官自执行的简单任务：入池后立即自取（主循环内联跑，不占操作员）
            for _d in outcome.tasks_detail:
                if _d.get("self") and _d.get("iid"):
                    _self_exec_pending.append(_d["iid"])
            commander_decisions.append(outcome)
            _parent_event(
                base,
                "plan_event",
                {
                    "decision": outcome.decision,
                    "tasks": outcome.n_tasks,
                    "beliefs": outcome.beliefs_applied,
                    "hypotheses": outcome.hypotheses_added,
                    "drift_rejected": outcome.drift_rejected,
                    "planned": outcome.tasks_detail[:8],
                    "wake": (signal.as_dict() if signal else {"kind": "plan"}),
                },
            )
            if outcome.decision == "deliver":
                # Commander 请求交付：交给交付门裁决（不直接结束）
                delivery_pending = True
            if outcome.beliefs_applied or outcome.hypotheses_added:
                watch.reset_stall()  # 有信念增量 → 停滞计数清零
            idle_rounds = 0 if outcome.n_tasks else idle_rounds + 1
            return outcome.n_tasks
        _before = {r["id"] for r in fr.list_all(status="open", limit=300)}
        n_new = await plan_round(
            objective,
            fr,
            planner_llm,
            model=model,
            next_depth=next_depth,
        )
        if n_new:
            _planned = [
                {"title": r["title"][:80], "lane": r.get("lane", "main")}
                for r in fr.list_all(status="open", limit=300)
                if r["id"] not in _before
            ]
            _parent_event(
                base,
                "plan_event",
                {
                    "decision": "continue",
                    "tasks": n_new,
                    "planned": _planned[:8],
                    "wake": (signal.as_dict() if signal else {"kind": "plan"}),
                },
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
                        lane=getattr(intent, "lane", "main") or "main",
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
                        lane=getattr(intent, "lane", "main") or "main",
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
        # 意图级步数/墙钟预算：硬止损（收益递减机械化）——意图预算优先于全局 profile；
        # 未设 max_wall_s 时读全局 SECPT_INTENT_WALL_S（长任务/0day 单面时间盒）
        _budget = getattr(intent, "max_steps", 0)
        _wall_budget = getattr(intent, "max_wall_s", 0)
        if not _wall_budget:
            with contextlib.suppress(ValueError):
                _wall_budget = int(os.environ.get("SECPT_INTENT_WALL_S", "0") or 0)
        # 团队模式 exploit/verify：挂 per-asset 完成广播让出钩子——
        # 同资产已被其他 worker 完成时，engine 注入协作广播引导收尾（治"通关后仍乱猜"）
        hook_factory = None
        if getattr(intent, "role", "") in ("exploit", "verify") and intent.asset_scope:

            def _make_yield_hook(inner, _s=stopper, _a=intent.asset_scope, _i=intent.id):
                return _YieldHook(inner, _s, _a, _i)

            hook_factory = _make_yield_hook
        _job = f"{base}-i{intent.id[-4:]}"
        # M1：同资产跨意图交接（通用：recon→exploit→verify、benchmark 多段、多轮重试）——
        # 上一意图的产出工作区产物复制到本工作区，并注入确定性 handoff（工作区清单 +
        # 上一轮结论）。没有这一步，每个新意图都是冷启动（1323 b 族每段重搭脚手架）。
        _asset_handoff = ""
        _asset = getattr(intent, "asset_scope", "") or ""
        if _asset:
            _prev = fr.asset_state(_asset)
            _prev_job = str((_prev or {}).get("last_job_id") or "")
            if _prev and _prev_job and _prev_job != _job:
                _manifest = _carry_ws_artifacts(_prev_job, _job)
                _asset_handoff = (
                    "【同资产前序工作交接（编排层确定性注入）】\n"
                    f"上一意图 job={_prev_job} status={_prev.get('last_status', '')}；"
                    f"产物已复制到本工作区：\n{_manifest}\n"
                    f"上一轮结论摘要：{str(_prev.get('summary') or '')[:600]}\n"
                    "优先复用已验证的入口/脚本/凭据，不要从零重探；旧结论只作线索，需自行验证。"
                )
        _obj = build_intent_objective(
            objective,
            intent,
            fr.recent_results(4),
            base=base,
            handoff=_asset_handoff,
            allow_logistics=bool(enable_logistics and getattr(intent, "lane", "main") == "main"),
        )
        # 教练介入：重试/RESET 前注入确定性纠偏，避免同一路线反复烧预算
        if int(getattr(intent, "attempts", 1) or 1) > 1:
            with contextlib.suppress(Exception):
                from secpt.core.orchestrator.coach import coach_for_retry

                _obj += "\n\n" + coach_for_retry(intent, fr.recent_results(4))
        _runtime_tools = (
            _make_delegate_logistics_tool(fr, base, intent)
            if enable_logistics and getattr(intent, "lane", "main") == "main"
            else None
        )

        def _exec(
            obj: str = _obj, job: str = _job, budget: int = _budget, wall: int = _wall_budget
        ) -> object:
            _steps = budget or default_max_steps  # S6：solve --steps 全局透传
            kw = {"max_steps": _steps} if _steps > 0 else {}
            if wall > 0:
                kw["max_wall_s"] = wall
            _tools_extra = [t for t in (intent.tools or "").split(",") if t.strip()]
            return run_engine(
                objective=obj,
                profile=profile,
                job_id=job,
                llm=llm,
                model_primary=_pick_intent_model(
                    getattr(intent, "role", ""), model, attempts=intent.attempts
                ),
                hook_factory=hook_factory,
                # G1 目标授权：objective 里的实际网络目标（start 返回的容器地址等）
                # ∪ host 形态资产 scope；asset_scope 题号不进白名单（1036 误拦教训）
                scope_entries=_intent_scope_entries(intent),
                # C2：意图级工具覆盖（按题族动态启用，如 web 族的 http_request/session）
                tools_extra=_tools_extra or None,
                runtime_tools=_runtime_tools,
                **kw,
            )

        result = await asyncio.to_thread(_exec)
        _last_job = result.job_id or _job
        # 子任务登记（P0-2）：父 job 无 events.jsonl（frontier 各意图跑在独立
        # 子 job 里），导出/评判全失效——settle 时增量聚合 + 收尾补漏
        if result.job_id:
            async with chains_lock:
                sub_jobs.append(result.job_id)
        # ⑦ 进展感知续步：预算打满但 ws 已有实质产出（攻击路径成型、只差收尾）→
        #    续新段（新 job_id -r2/-r3 独立事件流、不覆盖首段，产物由 P1-1 承接）。
        #    段预算递减（1323 复盘修正）：r2 = 原预算，r3 = 减半——多旗链式题的瓶颈
        #    是依赖链深度而非会话长度（b-01/b-02 续段步数收益率低），整额续第二段
        #    是时间黑洞（hard 6 旗 330×3=990 步烧穿整场 1/4 时限）。
        #    空转（无产出）不续，硬止损语义不变
        for _cont_i in range(2):
            if not (
                result.status == "step_exceeded"
                and _budget > 0
                and _has_ws_output(result.job_id or "")
            ):
                break
            _cont_budget = _budget if _cont_i == 0 else max(20, _budget // 2)
            _job2 = f"{_job}-r{_cont_i + 2}"
            # P1-1：续步必须承接产物——新 job_id 是独立工作区，若只换 id 不搬文件，
            # "继续原路径打穿"就是空话（1053 实跑：r2 ws 全空，模型只能重建 api.py 重探测）。
            # 复制旧 ws 产出（排除 transcript/outputs）到 r2 ws，并把真实清单注入 objective。
            _manifest = _carry_ws_artifacts(result.job_id or "", _job2)
            _obj2 = (
                _obj + f"\n\n【进展感知续步（编排层注入）】前 {_cont_i + 1} 段共 {result.steps} 步"
                f"已产出中间成果，已复制到本工作区（清单如下），"
                f"本轮 {_cont_budget} 步预算继续原攻击路径打穿，不要推倒重来：\n{_manifest}"
            )
            result2 = await asyncio.to_thread(_exec, _obj2, _job2, _cont_budget)
            if result2.job_id:
                _last_job = result2.job_id
                async with chains_lock:
                    sub_jobs.append(result2.job_id)
            if getattr(result2, "flag", ""):
                # 续跑拿到成果：采用续跑结果（事件已在 sub_jobs，资产检查保留首段 job_id）
                _jid_keep = result.job_id
                result = result2
                result.job_id = _jid_keep
                break
            # 续跑也未收尾：累计步数、状态接管（仍 step_exceeded 才会续下一段；
            # ANSWER 收尾/stall 等终态则停止续命， exhausted 止损语义不变）
            with contextlib.suppress(Exception):
                result.steps = int(result.steps) + int(result2.steps)
            result.status = result2.status
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
            _coach = ""
            with contextlib.suppress(Exception):
                from secpt.core.orchestrator.coach import coach_advice

                _coach = (
                    "\n\n"
                    + coach_advice(
                        intent.title,
                        status="stalled",
                        fail_reason=getattr(result, "status", "") or "stalled",
                        attempts=int(getattr(intent, "attempts", 1) or 1),
                        max_steps=int(getattr(intent, "max_steps", 0) or 0),
                        recent=fr.recent_results(4),
                        done_criteria=getattr(intent, "done_criteria", "") or "",
                    ).note
                )
            _obj_rs = (
                _obj + "\n\n【RESET 重建（编排层注入）】上一会话已策略级卡死（stalled），"
                "本会话从外部状态重建上下文，严禁原样重跑旧路线：\n"
                f"1. 上轮 handoff 摘要：\n{_handoff}\n"
                f"2. 已复制工作区产物（继续用，不要推倒重来）：\n{_manifest}\n"
                f"3. 已验证否定的路线（勿重复踩坑）：\n{_negs}\n"
                "基于以上状态选一条与上轮不同的路线推进，尽快以 ANSWER: 收尾。"
                f"{_coach}"
            )
            result_rs = await asyncio.to_thread(_exec, _obj_rs, _job_rs, _budget)
            if result_rs.job_id:
                _last_job = result_rs.job_id
                async with chains_lock:
                    sub_jobs.append(result_rs.job_id)
            try:
                from secpt.runtime.metrics import counter_inc

                counter_inc(
                    "loop_reset_total", outcome="recovered" if result_rs.flag else "exhausted"
                )
            except Exception:
                pass
            with contextlib.suppress(Exception):
                from secpt.runtime.notify import notify

                notify(
                    "job_reset",
                    f"secpt RESET 重建 {_job_rs}（意图：{getattr(intent, 'title', '')[:40]}）",
                    f"上轮 stalled 换会话重建，结果：{'拿到成果' if result_rs.flag else '未收尾'}",
                )
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
        elif (getattr(intent, "max_steps", 0) > 0 or getattr(intent, "max_wall_s", 0) > 0) and (
            result.status in ("step_exceeded", "wallclock_exceeded")
        ):
            # 意图级预算（步数/墙钟）是硬止损：不进自动复核，一次即终态——
            # 否则墙钟超限会换新预算再跑一遍，时间盒形同虚设
            _why_budget = (
                f"超意图步数预算（{intent.max_steps} 步硬止损）"
                if result.status == "step_exceeded"
                else f"超意图墙钟预算（{intent.max_wall_s}s 硬止损）"
            )
            status, why = "exhausted", _why_budget
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
        # M1：记录同资产最近工作区/结论（下一意图/下一轮据此交接）
        if _asset:
            fr.record_asset_state(
                _asset,
                job_id=_last_job,
                status=status,
                summary=(answer or why or f"steps={result.steps}"),
            )
        fr.settle(
            intent.id, status=status, result=answer or f"steps={result.steps}", fail_reason=why
        )
        # v4.2 轨迹层：结算事件入父流（哪个执行体做完了什么，对话面板可见）
        _parent_event(
            base,
            "intent_event",
            {
                "iid": intent.id,
                "title": intent.title,
                "status": status,
                "reason": (why or "")[:140],
                "lane": getattr(intent, "lane", "main"),
            },
        )
        # ── v4 值守层结算：失败类型化 + 确定性信念增量（Commander 唤醒时精结算）──
        if tree is not None:
            with contextlib.suppress(Exception):
                from secpt.core.orchestrator.watch import (
                    BUDGET_EXHAUSTED,
                    CAPABILITY_GAP,
                    NEGATIVE,
                    STUCK,
                    SUCCESS,
                    capability_gap,
                    classify_failure,
                    detect_capability_gaps,
                    detect_defensive_signals,
                )

                _kind = classify_failure(result.status, why)
                _hid = getattr(intent, "hypothesis", "") or ""
                _tail_outputs = [
                    str(m.get("content") or "")
                    for m in (getattr(result, "messages", None) or [])[-12:]
                    if m.get("role") == "tool"
                ]
                # v4.1 能力缺口出口：工具/凭据缺失进需求清单（Commander 唤醒时裁决：
                # 派安装任务 / request_tools / 放弃该路线），不再无声消失在输出里。
                # 两个口径：结算级 fail_reason + 工具输出扫描（真实场景缺失在输出里）
                _gap_hits = detect_capability_gaps(_tail_outputs) if _tail_outputs else []
                if _kind == CAPABILITY_GAP or capability_gap(why) or _gap_hits:
                    _snippet = next(
                        (
                            str(o)[:160]
                            for o in _tail_outputs
                            if any(
                                pat in o.lower()
                                for pat in (
                                    "command not found",
                                    "命令 '",
                                    "不存在",
                                    "未安装",
                                    "no such file",
                                    "permission denied",
                                )
                            )
                        ),
                        "",
                    )
                    _gap_txt = why[:120] or "（工具输出级缺口）"
                    tree.record_evidence_event(
                        _hid,
                        _last_job or intent.id,
                        f"[capability_gap] {intent.title}: {_gap_txt}｜输出：{_snippet}"
                        "（Commander 裁决：安装/request_tools/放弃该路线）",
                    )
                # v4.1 作战地图：对方防御反应检测（对方状态是地图的一半——
                # 反复 403/限流/WAF 说明打不动不是技术问题，Commander 应看到并改向）
                _defensive = detect_defensive_signals(_tail_outputs)
                if _defensive:
                    tree.record_evidence_event(
                        _hid,
                        _last_job or intent.id,
                        f"[defensive] 对方防御信号：{('、'.join(_defensive))[:160]}"
                        "（打不动可能不是技术问题，考虑改向/降速/换出口）",
                    )
                if _hid and tree.get(_hid) is not None:
                    if _kind == SUCCESS and status == "done":
                        # 判定实验通过：proposed→testing→confirmed（证据=子 job 工作区）
                        _node = tree.get(_hid)
                        if _node.state == "proposed":
                            tree.set_state(_hid, "testing")
                        with contextlib.suppress(Exception):
                            tree.set_state(_hid, "confirmed", evidence_ref=_last_job or "worker")
                    elif _kind in (NEGATIVE, BUDGET_EXHAUSTED):
                        # 负结果/预算耗尽：不迁移（Commander 裁决），落未消化证据
                        tree.record_evidence_event(
                            _hid,
                            _last_job or intent.id,
                            f"[{_kind}] {intent.title}: {why or '未达成'}",
                        )
                    elif _kind == STUCK:
                        tree.record_evidence_event(_hid, intent.id, "[stuck] 止损三档走完")
                elif _kind == STUCK:
                    # 无挂靠假设的卡死也留给 Coach/Commander（无 hid 事件）
                    tree.record_evidence_event("", intent.id, f"[stuck] {intent.title}")
        fr.reopen_ready()  # 本条结算后立即解锁依赖已满足的 blocked 意图（事件驱动，不等轮询）
        # 完成广播（双通道）：进程内 SharedStopper（快路径）+ 账本 scope_done
        # （③ 跨进程可见——其他 worker 进程轮询同一账本即可让出同资产在途意图）。
        # 加强①时序：settle 已释放资产租约，广播必须紧跟其后（在下面 on_settled
        # 等 await 之前置位）——同资产等待者的认领与"已完成跳过"之间不留 await 窗口
        if status == "done" and getattr(intent, "role", "") in ("exploit", "verify"):
            stopper.mark_done(intent.asset_scope, intent.id)
        scope_key = getattr(intent, "asset_scope", "") or ""
        # A1 语义修正：benchmark 的分段意图（role=""）完成只代表"一段旗拿到"，
        # 不代表整个资产完成——若照常广播 scope_done，同题的后续段/verify 会被
        # 误判"资产已完成"而让出/跳过。benchmark 下只让 exploit/verify 角色广播。
        _broadcast_scope = (
            status == "done"
            and scope_key
            and (profile != "benchmark" or getattr(intent, "role", "") in ("exploit", "verify"))
        )
        if _broadcast_scope:
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
                    lane=getattr(intent, "lane", "main") or "main",
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
                    lane=getattr(intent, "lane", "main") or "main",
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

    # 通用截止排空：显式 deadline 或 env SECPT_DEADLINE_TS（epoch 秒）
    _deadline = deadline_ts
    if _deadline is None:
        with contextlib.suppress(Exception):
            _deadline = float(os.environ.get("SECPT_DEADLINE_TS", "0") or 0) or None

    def _drain_now() -> bool:
        if should_drain is not None:
            with contextlib.suppress(Exception):
                if should_drain():
                    return True
        return bool(_deadline and time.time() >= _deadline - max(0.0, reserve_s))

    async def _worker_loop() -> None:
        nonlocal stop
        while not stop_ev.is_set():
            # A3：优雅排空——平台任务已结束/期限到 → 停止认领新意图，
            # 在途链自然收尾（不再开新题，避免无意义烧步）
            if _drain_now():
                break
            try:
                intent = fr.claim_next(lane="main")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 认领是 SQLite 写事务（BEGIN IMMEDIATE）：库被长写事务占住时
                # 会超时抛错。不能让 worker 协程因此死亡——否则在途意图无人
                # 结算，主循环永久等 wake，整个任务悬挂。
                print(f"[frontier] 主通道认领异常（2s 后重试）: {e}", flush=True)
                await asyncio.sleep(2.0)
                continue
            if intent is None:
                fr.reopen_ready()  # 无 open 可领 → 解锁依赖已满足的 blocked 意图
                await asyncio.sleep(0.5)
                continue
            if not fr.deps_satisfied(intent):  # D2.1 依赖未满足 → blocked（不空耗 attempts）
                fr.settle(intent.id, status="blocked", fail_reason="依赖未满足")
                fr.refund_attempt(intent.id)
                await asyncio.sleep(0.5)
                continue
            _parent_event(
                base,
                "intent_event",
                {"iid": intent.id, "title": intent.title, "status": "claimed", "lane": "main"},
            )
            try:
                ans = await _run_one(intent)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                ans = _settle_worker_crash(intent, e)
            if ans:
                _apply_answer(ans)

    def _settle_worker_crash(intent: Intent, exc: BaseException) -> None:
        """worker 逐意图异常兜底：结算 exhausted，防在途意图永久 running。

        _run_one 内部大部分失败已转成 status 返回；能走到这里的是编排层
        自身异常（DB 锁、回调炸、序列化失败等）。不结算会让主循环一直等
        settle 事件（实测：database is locked → 整任务悬挂）。
        """
        _why = f"worker 异常: {type(exc).__name__}: {exc}"
        print(f"[frontier] {_why}（意图 {intent.id} 结算为 exhausted）", flush=True)
        with contextlib.suppress(Exception):
            fr.settle(intent.id, status="exhausted", fail_reason=_why[:300])
        # 同步上下文无 await，可直接落链结果（无需 chains_lock）
        with contextlib.suppress(Exception):
            chains.append(
                ChainResult(
                    intent=intent.title,
                    role=getattr(intent, "role", "") or "",
                    asset_scope=getattr(intent, "asset_scope", "") or "",
                    status="exhausted",
                    steps=0,
                    depth=intent.depth,
                    source=intent.source,
                    lane=getattr(intent, "lane", "main") or "main",
                )
            )
        with contextlib.suppress(Exception):
            _parent_event(
                base,
                "intent_event",
                {
                    "iid": intent.id,
                    "title": intent.title,
                    "status": "failed",
                    "lane": getattr(intent, "lane", "main") or "main",
                    "error": _why[:300],
                },
            )
        with contextlib.suppress(Exception):
            wake_q.put_nowait(intent.id)
        return None

    async def _logistics_loop() -> None:
        """后台后勤通道：只领 logistics 任务，不参与主路径收敛。"""
        while not stop_ev.is_set():
            if _drain_now():
                break
            try:
                intent = fr.claim_next(lane="logistics")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[frontier] 后勤通道认领异常（2s 后重试）: {e}", flush=True)
                await asyncio.sleep(2.0)
                continue
            if intent is None:
                await asyncio.sleep(0.5)
                continue
            if not fr.deps_satisfied(intent):
                fr.settle(intent.id, status="blocked", fail_reason="依赖未满足")
                fr.refund_attempt(intent.id)
                await asyncio.sleep(0.5)
                continue
            _parent_event(
                base,
                "intent_event",
                {"iid": intent.id, "title": intent.title, "status": "claimed", "lane": "logistics"},
            )
            try:
                ans = await _run_one(intent)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                ans = _settle_worker_crash(intent, e)
            if ans:
                _apply_answer(ans)

    def _apply_answer(ans: str) -> None:
        """记录真实 flag；单题模式进入交付门而不是立即硬停。"""
        nonlocal stop, delivery_pending
        # 成果语义收窄（P0-1）：flags 只采信真实 flag 形态，ANSWER 全文（结论摘要/
        # benchmark 结算串）不计入跑分，防 unique_code 前缀污染。
        from secpt.core.orchestrator.flagsem import extract_real_flags

        real = extract_real_flags(ans)
        if real:
            flags.extend(v for v in real if v not in flags)
        if stop_on_first_flag:
            if enable_delivery:
                # 交付体先核验，不直接结束主进程；规划暂停，等主路径排空后 gate。
                delivery_pending = True
                with contextlib.suppress(Exception):
                    wake_q.put_nowait("delivery")
            else:
                stop = True
                stop_ev.set()

    def _drain_logistics_for_delivery() -> bool:
        """收敛排空（v4 值守层语义）：取消未开始的后勤任务；返回是否仍有在途后勤。

        解决收敛竞态：主路径排空时后勤可能还在飞——在途的等它回写（有界：
        后勤自带步数/墙钟预算），没开始的直接取消，不让粗活拖住收口。
        """
        if not enable_logistics:
            return False
        try:
            for row in fr.list_all(status="open", limit=200):
                if (row.get("lane") or "main") == "logistics":
                    fr.settle(
                        row["id"],
                        status="exhausted",
                        result="",
                        fail_reason="收敛排空：整体任务收口，未开始的后勤任务取消",
                    )
            return any(
                (r.get("lane") or "main") == "logistics"
                for r in fr.list_all(status="running", limit=200)
            )
        except Exception:
            return False

    async def _run_delivery_gate() -> bool:
        """Run the delivery agent. Returns True when the run should end."""
        nonlocal delivery_attempts, delivery_pending, delivery_wait, delivery_decision, idle_rounds
        from secpt.core.orchestrator.delivery_gate import DeliveryDecision, review_delivery

        if _drain_logistics_for_delivery():
            delivery_decision = DeliveryDecision(
                False, "收敛排空：等待在途后勤任务回写（有界，后勤自带预算）", waiting=True
            )
            await asyncio.sleep(1.0)
            return False
        decision = review_delivery(
            fr,
            base=base,
            objective=objective,
            flags=flags,
            attempt=delivery_attempts,
            max_attempts=max(0, int(delivery_max_attempts)),
            platform_verify=(profile == "benchmark"),
        )
        delivery_decision = decision
        # v4.1 信念回滚：复验尝试耗尽仍未通过 → 已 confirmed 的假设不可信度上调
        # （确认偏误的结构性对冲——交付被拒时信念必须跟着降，不能只有交付层知道）
        if tree is not None and flags and decision.accepted and "复验尝试已耗尽" in decision.reason:
            with contextlib.suppress(Exception):
                for _n in tree.nodes_by_state("confirmed"):
                    tree.update_confidence(_n.id, _n.confidence * 0.6)
                    tree.record_evidence_event(
                        _n.id, "delivery-gate", "[rollback] 交付复验未通过，置信度下调"
                    )
        if decision.waiting:
            # 主路径仍有在途/待执行任务；稍等，不要误收口也不要忙等。
            await asyncio.sleep(0.5)
            return False
        if (
            decision.accepted
            or delivery_attempts >= delivery_max_attempts
            or not decision.followups
        ):
            return True
        added = 0
        for followup in decision.followups:
            try:
                fr.add(
                    str(followup.get("title") or "交付复核"),
                    goal=str(followup.get("goal") or ""),
                    done_criteria=str(followup.get("done_criteria") or ""),
                    priority=int(followup.get("priority", 9)),
                    depth=int(followup.get("depth", 0)),
                    source=str(followup.get("source") or "delivery"),
                    lane=str(followup.get("lane") or "main"),
                    max_steps=int(followup.get("max_steps", 0) or 0),
                    max_wall_s=int(followup.get("max_wall_s", 0) or 0),
                )
                added += 1
            except Exception as e:
                print(f"[frontier] 交付 follow-up 入队失败（忽略）: {e}", flush=True)
        if added == 0:
            return True
        delivery_attempts += 1
        delivery_pending = False
        delivery_wait = True
        idle_rounds = 0
        with contextlib.suppress(Exception):
            wake_q.put_nowait("delivery-followup")
        return False

    if tree is not None:
        _n0 = _prune_dead_tasks()  # 启动修剪：resume 场景的死假设残留任务（先于 worker 认领）
        if _n0:
            print(f"[watch] 启动修剪：取消 {_n0} 条死假设挂靠任务", flush=True)

    main_workers = [asyncio.create_task(_worker_loop()) for _ in range(max_workers)]
    worker_tasks = list(main_workers)
    if enable_logistics:
        # 后勤并发（v4.1）：默认 2——下载与批量搜索常可并行；上限 4 防目标侧过载
        with contextlib.suppress(ValueError):
            _logi_workers = max(1, min(4, int(os.environ.get("SECPT_LOGISTICS_WORKERS", "2"))))
        for _ in range(_logi_workers):
            worker_tasks.append(asyncio.create_task(_logistics_loop()))

    # 事件驱动 planner：worker settle / 心跳 唤醒（对齐 ARTEX"数据库变化唤醒"）
    idle_rounds = 0
    rounds = 0
    while rounds < max_rounds and not stop:
        # 兜底监管：worker 协程若在循环外被不可恢复异常杀掉，主循环会永远等
        # wake（在途意图无人结算）。这里检测并收口，至少让任务正常落盘 summary。
        if main_workers and all(t.done() for t in main_workers) and not stop_ev.is_set():
            _errs = [
                str(t.exception())
                for t in main_workers
                if not t.cancelled() and t.exception() is not None
            ]
            print(
                f"[frontier] 主通道 worker 全部退出，任务收口：{'; '.join(_errs)[:200] or '无异常'}",
                flush=True,
            )
            stop = True
            stop_ev.set()
            break
        if _drain_now():
            stop_ev.set()
            break
        if fr.has_open_direct():
            # 模式②：只执行 direct；队列清空且无 running → 结束
            if fr.count_open(lane="main") == 0:
                if enable_delivery and flags and not delivery_decision:
                    if await _run_delivery_gate():
                        stop = True
                        stop_ev.set()
                        break
                    continue
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(wake_q.get(), timeout=2.0)
            continue
        main_open = fr.count_open(lane="main")
        starving = main_open < max_workers
        # ── v4 值守层触发器评估（确定性，便宜→贵）──
        if watch is not None:
            _total_wall = 0.0
            if _deadline:
                _total_wall = max(0.0, float(_deadline) - t0)
            for _sig in watch.budget_signals(
                elapsed_s=time.time() - t0,
                total_wall_s=_total_wall,
                used_intents=fr.total(),
            ):
                pending_signals.append(_sig)
            _pruned = _prune_dead_tasks()  # 死节点残留任务清理（确定性，逐轮）
            if _pruned:
                print(f"[watch] 内务修剪：取消 {_pruned} 条死假设挂靠任务", flush=True)
            _cons = watch.consolidation_signal()
            if _cons is not None:
                pending_signals.append(_cons)
            _stall = watch.stall_signal(
                done_intents=sum(
                    1
                    for c in chains
                    if (c.get("status") if isinstance(c, dict) else c.status) == "done"
                ),
                tree_transitions=sum(
                    tree.counts().get(k, 0) for k in ("confirmed", "refuted", "dead")
                )
                if tree is not None
                else 0,
            )
            if _stall is not None:
                pending_signals.append(_stall)
        # 是否还能产新意图（意图数达上限 / 规划连续两轮无产出 / 交付等待 → 收敛点）
        can_plan = (
            fr.total() < max_intents
            and idle_rounds < 2
            and not delivery_pending
            and not delivery_wait
        )
        # 值守层信号插队：有信号时无视 idle_rounds（停滞/预算/内务必须被 Commander 处理）
        _signal_plan = (
            bool(pending_signals)
            and fr.total() < max_intents
            and not (delivery_pending or delivery_wait)
        )
        if _signal_plan or (can_plan and (starving or main_open == 0)):
            await _maybe_plan()
            rounds += 1
            main_open = fr.count_open(lane="main")
            # 指挥官自执行的简单任务：派单入池后立即按 id 自取内联跑掉——
            # 不占操作员名额，走与操作员完全相同的 _run_one 结算路径（含假设信念更新）
            while _self_exec_pending:
                _siid = _self_exec_pending.pop(0)
                _si = fr.claim_by_id(_siid)
                if _si is None:
                    continue  # 已被他人领走或状态异常
                try:
                    _sans = await _run_one(_si)
                except asyncio.CancelledError:
                    raise
                except Exception as _se:
                    _sans = _settle_worker_crash(_si, _se)
                if _sans:
                    _apply_answer(_sans)
            main_open = fr.count_open(lane="main")
        # 交付门：有候选成果/复验等待/自然收敛时，由交付体决定是否结束。
        if enable_delivery and main_open == 0 and (delivery_pending or delivery_wait):
            if await _run_delivery_gate():
                stop = True
                stop_ev.set()
                break
            continue
        if main_open == 0:
            if fr.total() == 0 and rounds == 0:
                continue  # 首盘：等 worker 空转也无妨，继续 plan
            # 团队模式 legacy：verify/report 确定性补产（仅 role_plan 显式开启）
            if role_plan:
                if _queue_verify_if_needed(fr, base):
                    continue  # worker 认领 verify 意图执行（独立 PoC 复验）
                if _queue_report_if_needed(fr, objective):
                    continue  # worker 认领 report 意图执行；执行完再走收敛判定
            if fr.all_subgoals_settled():
                # v4 信念性收敛：假设树还有活动高价值节点且还能规划 → 唤醒 Commander
                # 处理"下一步打哪"，而不是因池空收口（受 stall_wake_budget 封顶）
                if (
                    tree is not None
                    and tree.open_high_value()
                    and stall_wake_budget > 0
                    and fr.total() < max_intents
                    and not (delivery_pending or delivery_wait)
                ):
                    stall_wake_budget -= 1
                    idle_rounds = 0
                    pending_signals.append(
                        _stall_signal_now("假设树仍有高价值未决节点（池空触发信念收敛检查）")
                    )
                    continue
                if enable_delivery and not delivery_decision:
                    if await _run_delivery_gate():
                        stop = True
                        stop_ev.set()
                        break
                    continue
                break  # Goals 覆盖度驱动收敛：所有子目标已结算（done/exhausted）
            # 复盘会 retro()（设计 §4.1）：空队列时先确定性路由——失败驱动局部递归
            # （§4.2）优先于"规划两轮无产出就收敛"。阶段由状态定：recurse 时编排层
            # 直接产结构化子意图（不经 planner LLM），防"planner 忘了展开"时系统卡死
            if (
                os.environ.get("SECPT_ROLE_TEMPLATES", "0") == "1"
                and retro(fr, base=base) == "recurse"
                and _expand_recurse(fr, base=base, max_intents=max_intents) > 0
            ):
                continue  # 子意图已入队，worker 认领执行（legacy 角色递归）
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
                # v4：无产出唤醒前给信念收敛检查一次机会（活动假设仍在 → 让 Commander 决定）
                if (
                    tree is not None
                    and tree.open_high_value()
                    and stall_wake_budget > 0
                    and fr.total() < max_intents
                    and not (delivery_pending or delivery_wait)
                ):
                    stall_wake_budget -= 1
                    idle_rounds = 0
                    pending_signals.append(_stall_signal_now("规划无产出，但假设树仍有未决方向"))
                    continue
                if enable_delivery and not delivery_decision:
                    if await _run_delivery_gate():
                        stop = True
                        stop_ev.set()
                        break
                    continue
                break  # 空队列 + 无法再规划（意图数达上限/规划两轮无产出）→ 收敛
            timeout = 2.0  # 空队列短心跳（测试友好；实战 2s 无害）
        else:
            timeout = float(os.environ.get("SECPT_FRONTIER_HEARTBEAT", "60"))
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(wake_q.get(), timeout=timeout)

    stop_ev.set()
    await asyncio.gather(*worker_tasks, return_exceptions=True)

    # P0-2：子 job 事件流聚合到父 job（否则父 job 无 events.jsonl，
    # watch 导出 / 评判 evaluate_job 全部失效）。final=True 补漏未增量聚合的
    # 子 job + 追加 summary 事件（增量聚合期间已合并的由 aggregated 幂等跳过）
    _aggregate_sub_events(base, sub_jobs, chains, flags, aggregated=aggregated)

    _release_task_lock(_lock)  # 加强④：正常路径显式释放（异常路径 fd 回收兜底）

    summary = RunSummary(
        job_id=base,
        mode="frontier",
        flags=flags,
        chains=chains,
        open_remaining=fr.count_open(),
        wall_s=round(time.time() - t0, 1),
        extras={
            "done_events": done_events,  # 完成广播事件（含 report 聚合条目）
            "sub_jobs": sub_jobs,  # 子任务清单（父事件流聚合来源）
            "delivery": delivery_decision.as_dict() if delivery_decision is not None else None,
            "logistics_open": fr.count_open(lane="logistics"),
            "logistics_enabled": bool(enable_logistics),
            "commander_wakes": len(commander_decisions),
            "commander_deliver_requested": any(
                getattr(d, "decision", "") == "deliver" for d in commander_decisions
            ),
            "hypothesis_counts": tree.counts() if tree is not None else {},
        },
    ).to_dict()
    # 主智能体收尾（经验蒸馏由 solve 的 learning 链路负责，这里落审计摘要）
    retro_path = _write_retro(
        base=base,
        objective=objective,
        flags=flags,
        chains=chains,
        delivery=delivery_decision,
        fr=fr,
        wall_s=summary.get("wall_s", 0.0),
    )
    if retro_path is not None:
        summary["retro_path"] = str(retro_path)
    return summary
