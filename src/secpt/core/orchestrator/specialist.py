"""S5 Specialist 编排：四角色 = 四次 Engine.run()，只是 profile 片段不同。

    recon   → 枚举资产/端口/服务/目录，产出 structured asset list（写 ws/assets.json）
    exploit → 针对一个具体入口一条链（并行单位，黑盒利用）
    analyze → 白盒场景：读源码 / 数据流追踪（对应 codeaudit，替代 exploit）
    verify  → S6 实现，本阶段占位（evidence 非空即乐观确认 verified=1）

红线：
- Orchestrator 不许读取 Engine 内部的 messages（只读产物文件 + warroom 表行）
- 角色之间只交换 warroom 表行，不交换自然语言摘要
- 每个角色内部是完整不可断裂的单链（S0 主循环语义原样保留）

warroom 接入：WarroomHooks 包装 EventSink/Watchdog 链，在 Engine 的
on_tool / on_result / on_finding 观察点把行为落进四张表。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from secpt.core.job.event import EventSink
from secpt.core.job.models import JobRecord, TaskSpec
from secpt.core.job.resume import jobs_dir
from secpt.core.job.store import JobStore
from secpt.core.memory.warroom import Warroom
from secpt.core.profile.loader import load_profile
from secpt.core.profile.render import render_system_extra
from secpt.core.watchdog import Watchdog
from secpt.runtime import engine as _engine

# 每角色独立 round 基准：recon=0，exploit-i=1000*(i+1)。用于 events/checkpoint 区分
RECON_ROUND_BASE = 0
EXPLOIT_ROUND_BASE = 1000

# recon 单独小预算（比 exploit 短：它只负责枚举，不该烧整链预算）
RECON_STEPS_DIV = 5
RECON_WALL_DIV = 4


def _parse_asset_url(url: str) -> tuple[str, int | None, str]:
    """从 URL 提取 (host, port, service)。解析失败返回空值。"""
    try:
        u = urlparse(url)
        return u.hostname or "", u.port, u.scheme or ""
    except Exception:
        return "", None, ""


def _render_assets(assets: list[dict]) -> str:
    """把 warroom.assets 渲染成 exploit 上下文文本行（交换表行，非摘要）。"""
    if not assets:
        return "（暂无已确认资产）"
    lines = []
    for a in assets:
        lines.append(
            f"- host={a.get('host', '')} port={a.get('port') or ''} "
            f"service={a.get('service', '')} tech={a.get('tech', '')}"
        )
    return "\n".join(lines)


class WarroomHooks:
    """把 Engine 的观察点写进 warroom 四张表，并透传内层 hooks 链。

    链：Engine → WarroomHooks → Watchdog → EventSink
    - on_tool / on_result   → attempts（key=sha1(action+target)，重复跳过）
    - on_finding            → credentials（credential）/ assets（URL 解析）
    - 其余 hooks 经 __getattr__ 透传给内层（Watchdog）
    """

    def __init__(self, inner, warroom: Warroom) -> None:
        self._inner = inner
        self._warroom = warroom
        # PR-A：按 call_id 精确关联（并行多链下 FIFO 顺序会被其他链交错打乱；
        # LLM 产出的 call_id 全局唯一，dict 映射天然并发安全）
        self._pending: dict[str, tuple[str, str]] = {}

    def __getattr__(self, name):
        return getattr(self._inner, name)

    # ---------------- attempts ----------------

    def on_tool(self, call_id: str, name: str, args: dict) -> None:
        action = f"{name}({json.dumps(args, ensure_ascii=False)[:200]})"
        target = _extract_target(args)
        self._pending[call_id] = (action, target)
        try:
            self._warroom.add_attempt(action, target=target)
        except Exception as e:
            print(f"[warroom] add_attempt failed (ignored): {e}")
        inner = getattr(self._inner, "on_tool", None)
        if inner is not None:
            inner(call_id, name, args)

    def on_result(self, call_id: str, out: str) -> None:
        pending = self._pending.pop(call_id, None)
        if pending is not None:
            action, target = pending
            # 补记结果：UPDATE 已有行（INSERT OR IGNORE 撞 UNIQUE 后 result 不落盘）
            try:
                self._warroom.update_attempt_result(action, target=target, result=out[:1000])
            except Exception as e:
                print(f"[warroom] update attempt failed (ignored): {e}")
        inner = getattr(self._inner, "on_result", None)
        if inner is not None:
            inner(call_id, out)

    # ---------------- findings ----------------

    def on_finding(self, kind: str, value: str, evidence: str = "") -> None:
        try:
            if kind == "credential":
                self._warroom.add_credential("regex", value, source_step=evidence)
                self._warroom.add_finding(
                    f"cred-{self._warroom.count_attempts() + 1}",
                    "credential",
                    value,
                    evidence=evidence,
                    severity="medium",
                )
            elif kind == "asset":
                host, port, service = _parse_asset_url(value)
                if host:
                    self._warroom.add_asset(host, port, service, "", source_step=evidence)
            elif kind == "flag":
                self._warroom.add_finding(
                    f"flag-{self._warroom.count_attempts() + 1}",
                    "flag",
                    value,
                    evidence=evidence,
                    severity="critical",
                )
        except Exception as e:
            print(f"[warroom] on_finding failed (ignored): {e}")
        inner = getattr(self._inner, "on_finding", None)
        if inner is not None:
            inner(kind, value, evidence)
        elif hasattr(self._inner, "finding"):
            self._inner.finding(kind, value, evidence)


def _extract_target(args: dict) -> str:
    """从工具参数里尽力提取 target（URL / 路径 / 命令里的地址），供 attempts 去重。"""
    if not isinstance(args, dict):
        return ""
    for key in ("url", "host", "target", "command"):
        v = str(args.get(key, "") or "")
        if not v:
            continue
        for tok in v.split():
            if tok.startswith("http://") or tok.startswith("https://"):
                return tok
            if tok.startswith(("127.", "10.", "192.168.", "172.")):
                return tok
        return v[:120]
    return ""


@dataclass
class SpecialistResult:
    """单角色 Engine 运行结果（orchestrator 汇总用）。"""

    role: str
    entry_index: int = -1
    flag: str | None = None
    steps: int = 0
    wall_s: float = 0.0
    status: str = "failed"
    assets_written: list[dict] = field(default_factory=list)


class SpecialistOrchestrator:
    """一次多入口任务的编排：recon → [exploit × N 并行] → verify。

    profile 由 spec.task_type 决定（ctf/pentest/codeaudit/...）；
    llm 可注入（SECPT_FAKE_LLM=1 时用 FakeLLM，无 key 可测）。
    """

    def __init__(
        self,
        spec: TaskSpec,
        *,
        store: JobStore | None = None,
        llm=None,
        db_path: str = "var/secpt.sqlite",
    ) -> None:
        self.spec = spec
        self.job_id = spec.job_id
        self.profile = spec.task_type
        self.db_path = db_path
        self.store = store or JobStore(db_path)
        jdir = jobs_dir() / self.job_id
        jdir.mkdir(parents=True, exist_ok=True)
        self.jdir = jdir
        # 事件流：整 job 共享一个 sink（多链 append 同文件，round 偏移区分）
        self.sink = EventSink(self.job_id, jobs_dir(), store=self.store)
        self.warroom = Warroom(jdir / "warroom.sqlite", job_id=self.job_id)
        self.llm = self._default_llm(llm)

    # ---------------- 基础设施 ----------------

    def _default_llm(self, llm):
        if llm is not None:
            return llm
        if os.environ.get("SECPT_FAKE_LLM"):
            from secpt.core.job.fakellm import FakeLLM

            return FakeLLM(n_steps=3, job_id=self.job_id)
        from secpt.runtime.llm.client import call_llm

        return call_llm

    def _make_cfg(
        self,
        *,
        role: str,
        objective_extra: str,
        ws_name: str,
        round_offset: int,
    ) -> _engine.RunConfig:
        """按角色构建 RunConfig：独立 workspace + 阶段化 objective + profile system_extra。"""
        prof = load_profile(self.profile)
        objective = f"{self.spec.objective}\n\n{objective_extra}"

        cfg = _engine.RunConfig(
            job_id=self.job_id,
            objective=objective,
            system_extra="",  # 下面渲染
            workspace=self.jdir / "agents" / ws_name,
            model_primary="deepseek-chat",
            model_fallback="deepseek-reasoner",
            max_steps=prof.budget.steps,
            max_wall_s=int(prof.budget.wall_s),
            flag_regex=prof.terminator.flag_regex,
            round_offset=round_offset,
            agent_id=role,  # 事件 trace：结构化日志按角色检索（recon/exploit/verify/report）
        )
        # profile 级 bash 工具超时覆盖（DAG 各 agent 与单链一致）
        if getattr(prof, "bash_timeout_s", 0) > 0:
            cfg.bash_timeout_s = prof.bash_timeout_s
        cfg.bash_whitelist = frozenset(_engine.DEFAULT_BASH_WHITELIST) | frozenset(
            prof.merged_whitelist_extra()
        )
        constraints = list(prof.constraints) or ["不要扫描题面外的网段。"]
        if prof.terminator.stop_condition:
            constraints.append(prof.terminator.stop_condition)
        cfg.system_extra = render_system_extra(
            self.profile,
            objective=objective,
            tools_doc=_engine.build_tools_doc(cfg),
            prior=prof.prior_text(),
            role=prof.role,
            constraints="\n".join(constraints),
        )
        return cfg

    def _hooks(self, warroom: Warroom | None = None) -> WarroomHooks:
        """构造 hooks 链：WarroomHooks(Watchdog(EventSink))。"""
        watchdog = Watchdog(self.sink, store=self.store, job_id=self.job_id)
        return WarroomHooks(watchdog, warroom or self.warroom)

    def _run_engine(self, cfg: _engine.RunConfig, hooks) -> _engine.RunResult:
        return asyncio.run(_engine.Engine(cfg, llm=self.llm, hooks=hooks).run())

    # ---------------- recon ----------------

    def run_recon(self) -> list[dict]:
        """枚举资产：模型把 structured asset list 写 ws/assets.json，orchestrator 读文件入库。

        产物文件是角色间交换的**表行数据**（结构化），不算读取 Engine 内部 messages。
        """
        prof = load_profile(self.profile)
        recon_steps = max(10, prof.budget.steps // RECON_STEPS_DIV)
        recon_wall = max(120, int(prof.budget.wall_s) // RECON_WALL_DIV)
        entries = "\n".join(f"- {e}" for e in self.spec.entries)

        objective_extra = (
            "【当前阶段：资产侦察（recon）】\n"
            f"初始目标入口：\n{entries}\n\n"
            "先对目标做资产枚举：端口 / 服务 / 目录 / 技术栈。\n"
            "侦察结束前，把结构化资产清单写入文件 ws/assets.json（JSON 数组，"
            "每项含 host/port/service/tech 四个字段，没有的字段给空字符串或 null）。\n"
            "没有侦察到资产时也写一个空数组 []。"
        )
        cfg = self._make_cfg(
            role="recon",
            objective_extra=objective_extra,
            ws_name="recon",
            round_offset=RECON_ROUND_BASE,
        )
        cfg.max_steps = min(cfg.max_steps, recon_steps)
        cfg.max_wall_s = min(cfg.max_wall_s, recon_wall)

        self.sink.status_change("running", round_=RECON_ROUND_BASE)
        result = self._run_engine(cfg, self._hooks())

        assets = self._load_assets(cfg.work_dir)
        for a in assets:
            try:
                self.warroom.add_asset(
                    host=str(a.get("host", "")),
                    port=a.get("port"),
                    service=str(a.get("service", "")),
                    tech=str(a.get("tech", "")),
                    source_step="recon assets.json",
                )
            except Exception as e:
                print(f"[warroom] recon asset failed (ignored): {e}")
        print(f"[orchestrator] recon 完成: steps={result.steps} 资产={len(assets)} 条")
        return assets

    @staticmethod
    def _load_assets(work_dir: Path) -> list[dict]:
        """读 recon 写出的 ws/assets.json；不存在/解析失败返回空列表。"""
        p = work_dir / "assets.json"
        if not p.is_file():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
        except Exception as e:
            print(f"[orchestrator] 解析 assets.json 失败(忽略): {e}")
            return []

    # ---------------- exploit / analyze ----------------

    def run_chain(self, entry_index: int, entry: str, assets: list[dict]) -> SpecialistResult:
        """对单个入口跑一条链。codeaudit 用 analyze 角色（白盒），否则 exploit。"""
        is_audit = self.profile in ("codeaudit",)
        role = "analyze" if is_audit else "exploit"
        asset_text = _render_assets(assets)

        if is_audit:
            stage = (
                "【当前阶段：白盒源码审计（analyze）】\n"
                f"针对入口 {entry} 读源码 / 追踪危险数据流（sink），"
                "定位可利用漏洞并给出利用路径。\n"
                f"已确认资产：\n{asset_text}\n\n"
                "确认漏洞后输出 ANSWER: <结论>。"
            )
        else:
            stage = (
                f"【当前阶段：定向利用（exploit entry#{entry_index}）】\n"
                f"针对入口 {entry} 做定向利用。\n"
                f"已确认资产：\n{asset_text}\n\n"
                "利用成功（拿到 flag 或确认漏洞）后输出 ANSWER: <结论>。"
            )

        cfg = self._make_cfg(
            role=role,
            objective_extra=stage,
            ws_name=f"{role}-{entry_index}",
            round_offset=EXPLOIT_ROUND_BASE * (entry_index + 1),
        )
        result = self._run_engine(cfg, self._hooks())
        return SpecialistResult(
            role=role,
            entry_index=entry_index,
            flag=result.flag,
            steps=result.steps,
            wall_s=result.wall_s,
            status=result.status,
        )

    # ---------------- verify（S5 占位） ----------------

    def run_verify(self) -> int:
        """S5 占位：evidence 非空的 finding 乐观确认 verified=1。

        S6 将替换为：全新 Engine + 全新 workspace + 全新 HTTP session，
        严格按 finding 描述的步骤独立重做一遍，成功才 verified=1。
        """
        n = 0
        for f in self.warroom.list_findings():
            if not f["verified"] and (f["evidence"] or "").strip():
                self.warroom.mark_verified(f["id"], True)
                n += 1
        return n

    # ---------------- 主流程 ----------------

    async def run(self) -> dict:
        """多入口 DAG：recon → [exploit × N 并行] → verify。返回汇总 dict。"""
        t0 = time.time()
        rec = self.store.get_job(self.job_id)
        if rec is None:
            rec = JobRecord(job_id=self.job_id, spec=self.spec, status="running")
        else:
            rec.set_status("running")
        self.store.upsert_job(rec)

        # recon 也是同步的 Engine.run()，必须在事件循环外跑（与 exploit 链一致用 to_thread）
        assets = await asyncio.to_thread(self.run_recon)
        if not assets:
            # recon 没产出 → 回退：入口本身作为资产上下文
            assets = [
                {"host": e, "port": None, "service": "", "tech": ""} for e in self.spec.entries
            ]

        # 并行 exploit/analyze 链（进程内 asyncio 并行；bash 阻塞由 LLM await 交错）
        chains = await asyncio.gather(
            *[
                asyncio.to_thread(self.run_chain, i, e, assets)
                for i, e in enumerate(self.spec.entries)
            ]
        )

        verified = self.run_verify()

        rec = self.store.get_job(self.job_id) or rec
        rec.steps = sum(c.steps for c in chains)
        rec.round = max(rec.round, len(chains) * EXPLOIT_ROUND_BASE)
        flags = [c.flag for c in chains if c.flag]
        if flags:
            rec.set_status("succeeded")
            for i, fl in enumerate(flags):
                self.sink.finding("flag", "flag", fl, "orchestrator")
                # S5 占位：链自宣告的 flag 登记为 finding，交给 verify 乐观确认
                self.warroom.add_finding(
                    f"flag-chain-{i}",
                    "flag",
                    fl,
                    evidence=f"exploit entry#{i} 链自宣告",
                    severity="critical",
                )
            verified = self.run_verify()  # 新登记的 flag 也纳入乐观确认
        else:
            rec.set_status("failed")
        self.store.upsert_job(rec)
        self.sink.status_change(rec.status)

        wall = time.time() - t0
        summary = {
            "job_id": self.job_id,
            "entries": list(self.spec.entries),
            "recon_assets": len(assets),
            "warroom_assets": self.warroom.count_assets(),
            "warroom_attempts": self.warroom.count_attempts(),
            "findings": len(self.warroom.list_findings()),
            "verified": verified,
            "flags": flags,
            "chains": [
                {
                    "role": c.role,
                    "entry": c.entry_index,
                    "flag": c.flag,
                    "steps": c.steps,
                    "wall_s": round(c.wall_s, 1),
                    "status": c.status,
                }
                for c in chains
            ],
            "wall_s": round(wall, 1),
        }
        print(
            f"\n[orchestrator] 完成 job={self.job_id} wall={wall:.1f}s "
            f"assets={summary['warroom_assets']} attempts={summary['warroom_attempts']} "
            f"verified={verified} flags={flags}"
        )
        return summary
