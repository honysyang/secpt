"""CLI 唯一入口：secpt run / chat / resume / tools / evidence（§6.3 五命令）+ panel（W3-4）。

装配与兜底纪律：
- main() 是库层异常的第一兜底（§9：库层不吞错、统一上抛）——SecptError 落一行
  结构化 JSON 进 stderr 后非零退出，绝不带 traceback、绝不让循环悬死。
- provider 装配（诚实边界）：``LLM_PROVIDER=mock`` 出厂脚本离线全流程演示；
  ``openai`` 档（M4-1 起）从 SECPT_LLM_* 环境变量装配真 provider，缺项显式
  E_CONFIG 拒跑，绝不静默降 mock——假聪明比真失败更危险。
- 环境变量：``SECPT_DB``（默认 ./secpt.db）、``SECPT_RAW_DIR``（默认 ./raw）、
  ``SECPT_NMAP_BIN``（默认 /usr/bin/nmap；演示环境可指向占位路径强制回执）、
  ``SECPT_PROFILE``（默认 ./configs/profile.toml；文件缺失按拍板③降关闭 + audit 落痕）。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from functools import partial

from secpt.core.loop import LoopResult, PentestLoop
from secpt.core.mission_board import MissionBoard
from secpt.errors import SecptError
from secpt.finalize.exit_checklist import ExitChecklist
from secpt.finalize.experience import Distiller as ExperienceDistiller
from secpt.finalize.reporter import Reporter, render_markdown
from secpt.judge import load_profile, note_for, sweep
from secpt.llms.client import (
    TEMPERATURE_FREESTYLE,
    LlmClient,
    LlmProvider,
    Message,
    StepOutput,
    ToolCall,
)
from secpt.llms.coach import Coach
from secpt.llms.fake import FakeLLM
from secpt.llms.main_llm import MainLlm
from secpt.llms.providers import OpenAICompatProvider
from secpt.llms.worker import Worker
from secpt.ob.cli_view import CliView, configure_logging, evidence_report
from secpt.ob.events import EventBus
from secpt.ob.web_view import create_app
from secpt.store import ledger
from secpt.store.migrate import connect
from secpt.tools.adapters import AgentRunTool, DeclareFindingTool, NmapTool
from secpt.tools.executors import ArgvRunner, DockerRunner, load_isolation_config
from secpt.tools.guard import Approver, AutoApprover, CliApprover, load_guard_config
from secpt.tools.mcp_client import build_mcp_adapters, load_mcp_config
from secpt.tools.probe import (
    CaptureBackend,
    build_backend,
    load_probe_config,
)
from secpt.tools.probe import (
    note_for as probe_note_for,
)
from secpt.tools.registry import ToolAdapter, ToolRegistry

_NULL_TARGET = "(demo)"
_DEFAULT_WORKERS = 3
_DEFAULT_WORKER_TIMEOUT_S = 300.0


def _provider_name() -> str:
    """LLM provider 档位名（M4-1：mock | openai；未知值装配期 E_CONFIG）。"""
    return os.environ.get("LLM_PROVIDER", "mock").strip() or "mock"


def _openai_provider() -> OpenAICompatProvider:
    """从环境变量装配 openai 兼容 provider；缺项显式 E_CONFIG（不静默降 mock）。

    密钥只在内存中流转：不落配置文件、不进日志、不进 audit（异常 context 的
    响应体片段在 provider 层已脱敏）。base_url 是服务根（可带 /v1），
    请求打 {base_url}/chat/completions。
    """
    base_url = os.environ.get("SECPT_LLM_BASE_URL", "").strip()
    api_key = os.environ.get("SECPT_LLM_API_KEY", "").strip()
    model = os.environ.get("SECPT_LLM_MODEL", "").strip()
    missing = [
        key
        for key, value in (
            ("SECPT_LLM_BASE_URL", base_url),
            ("SECPT_LLM_API_KEY", api_key),
            ("SECPT_LLM_MODEL", model),
        )
        if not value
    ]
    if missing:
        raise SecptError(
            "LLM_PROVIDER=openai 缺少必需环境变量（不静默降 mock）",
            code="E_CONFIG",
            context={"missing": missing},
        )
    return OpenAICompatProvider(base_url=base_url, api_key=api_key, model=model)


def _provider_for(mock_factory: Callable[[], LlmProvider]) -> LlmProvider:
    """provider 装配分发（M4-1 诚实边界）：mock 出厂脚本 / openai 真档显式二选一。

    openai 档缺 env → E_CONFIG 拒跑；未知档名 → E_CONFIG——绝不静默降 mock。
    分发收敛在此一处，三处调用点（主循环/对话/worker）不再各抄一份分支。
    """
    name = _provider_name()
    if name == "openai":
        return _openai_provider()
    if name != "mock":
        raise SecptError(
            "未知 LLM_PROVIDER（可选 mock/openai）", code="E_CONFIG", context={"provider": name}
        )
    return mock_factory()


def _main_provider(goal_text: str) -> LlmProvider:
    """主循环 provider：mock=出厂演示脚本；openai=env 装配的真 provider。"""
    return _provider_for(lambda: _mock_run_script(goal_text))


def _chat_provider() -> LlmProvider:
    """对话模式 provider：mock=出厂直答脚本；openai=真模型单轮直答（不进闭环）。"""
    return _provider_for(_chat_mock_script)


def _worker_provider(index: int) -> LlmProvider:
    """worker provider：mock=单步收工脚本；openai=每线程独立真 provider 实例。"""
    return _provider_for(lambda: _worker_mock_script(index))


def _mock_run_script(goal_text: str) -> FakeLLM:
    """M1 出厂演示脚本：进场三问 → 一步 nmap 摸扫 → 一步收工（done）→ 报告摘要。

    脚本内容与 goal 文本无关——FakeLLM 不真理解输入，target 取输入截断只是
    让账本可读；nmap 由 env 指向占位二进制必落 E_TOOL_FAIL 回执，整程零网络。
    尾项是 LLMA 报告的 summary 脚本（run 正常收束后由 _emit_report 消费）；
    resume 不跑进场（无 goal 项），直接收工 + 摘要。
    """
    goal_json = json.dumps(
        {
            "target": (goal_text[:60] or _NULL_TARGET),
            "objective": "M1 mock 演示：主循环全流程走通",
            "success_criteria": "小票落账、水位线推进、出口收束",
        },
        ensure_ascii=False,
    )
    sweep = StepOutput(
        thought="摸扫一圈端口（演示步）",
        tool_calls=[ToolCall(name="nmap", args={"target": _NULL_TARGET, "ports": "22,80"})],
    )
    wrap_up = StepOutput(thought="演示收工", tool_calls=[], done=True)
    summary = json.dumps(
        {"summary": "M1 mock 演示：nmap 摸扫一轮（工具回执已落账），出口三验通过。"},
        ensure_ascii=False,
    )
    # M3-5 领单演示（env 门控）：SECPT_DEMO_AGENT_RUN=1 时插一步 agent_run，
    # 让"主循环 → 任务板 → worker → 收拢小票"整链在 mock 离线一次跑通；
    # 未开时脚本与 M1/M2 逐字节一致（老演示不回退）。
    agent_step = StepOutput(
        thought="派一单给 worker（M3-5 领单演示）",
        tool_calls=[
            ToolCall(
                name="agent_run",
                args={
                    "mission": "演示单：摸扫一圈并如实回执",
                    "persona": "recon",
                    "step_budget": "2",
                },
            )
        ],
    )
    mid = [sweep, agent_step] if _demo_agent_run_enabled() else [sweep]
    if goal_text:
        if _demo_agent_run_enabled():
            # agent_run 成功小票会成为收尾蒸馏候选（experience.py 的候选口径），
            # 因此演示脚本多备一条"空草案"应答——蒸馏器真跑一次、如实落 0 条。
            distill_stub = json.dumps({"entries": []}, ensure_ascii=False)
            return FakeLLM([goal_json, *mid, wrap_up, distill_stub, summary])
        return FakeLLM([goal_json, *mid, wrap_up, summary])
    return FakeLLM([wrap_up, summary])


def _demo_agent_run_enabled() -> bool:
    """M3-5 演示开关（env）：默认关，保持 M1/M2 出厂脚本原样。"""
    return os.environ.get("SECPT_DEMO_AGENT_RUN", "").strip().lower() in ("1", "true", "yes")


def _chat_mock_script() -> FakeLLM:
    """对话模式出厂脚本：单轮直答，不进闭环、不落账（WF1）。"""
    return FakeLLM(
        [
            "（M1 mock）对话模式不进闭环：本回复由 FakeLLM 出厂脚本生成；"
            "接真实 provider 后由模型作答。"
        ]
    )


def _worker_mock_script(index: int) -> FakeLLM:
    """worker 出厂脚本（M3-5 mock 路径）：单步收工，收拢摘要带工号。

    为什么不在演示 worker 里仿造 nmap/declare_finding：mock 无网络，伪造证据
    会破"raw_ref 必指真文件"的诚实锚；worker 的能力链路由 e2e 测试用预置
    证据文件覆盖，CLI 演示只需证明"领单—收单—小票回主账"这条线是活的。
    """
    step = StepOutput(
        thought=f"worker-{index} 领单完成（mock 演示：无网络动作，不伪造证据）",
        tool_calls=[],
        done=True,
    )
    return FakeLLM([step])


def _worker_builder() -> Callable[[int], Worker]:
    """worker 装配工厂：每线程一次构建（独立 conn + 独立 registry + 独立 LLM）。

    两条硬约束落在这里：
    - 独立 conn：sqlite 连接不跨线程共享，各线程直写同一文件库（拍板③）；
    - registry 不含 agent_run：worker 无从再派单，套娃死锁从构造上不存在。

    Guard 同权（§3.6）但 approver=None：后台线程没有人工通道，HIGH 级动作
    fail-closed——worker 不得替人点头，这与主循环 CliApprover 的口径一致。
    """
    _worker_provider(0)  # 装配期试装：openai 档缺 env 的 E_CONFIG 必须在主线程炸，
    # 不能等 worker 线程上岗才成哑弹（builder 是装配期职责，§66 记档口径）。
    raw_dir = os.environ.get("SECPT_RAW_DIR", "raw")
    binary = os.environ.get("SECPT_NMAP_BIN", "/usr/bin/nmap")
    db_path = os.environ.get("SECPT_DB", "secpt.db")
    runner = _runner()
    guard = load_guard_config(_profile_path())
    probe = _probe_backend()
    # MCP adapter 装配期一次、各 worker 线程共享：McpTool 无状态（一发一会话），
    # registry 对 adapter 只读 manifest + 调 run，跨线程安全。
    mcp = _mcp_adapters()

    def build(index: int) -> Worker:
        return Worker(
            llm=LlmClient(_worker_provider(index)),
            dispatcher=ToolRegistry(
                [
                    NmapTool(binary=binary, raw_dir=raw_dir, runner=runner),
                    DeclareFindingTool(),
                    *mcp,
                ],
                guard=guard,
                approver=None,
                probe=probe,
            ),
            conn=connect(db_path),
        )

    return build


def _runner() -> ArgvRunner | None:
    """M3-5 执行隔离：docker 档包 DockerRunner，shell 档 None（LocalRunner 默认）。

    fail-closed 语义在 adapters 层：docker 不可用 → E_TOOL_FAIL 回执，
    绝不静默退回本机执行（宿主方案不可信）。
    """
    cfg = load_isolation_config(_profile_path())
    if cfg.isolation != "docker":
        return None
    return DockerRunner(image=cfg.image, docker_bin=cfg.docker_bin)


def _board(conn: sqlite3.Connection) -> MissionBoard:
    """领单任务板装配：默认 3 worker（SECPT_WORKERS 可调），锚点走最近 task。

    为什么锚点用 ledger.latest_task：agent_run 是主循环里的同步派单，此刻
    必然已有当前 task；解析失败（空库）由 submit 包成 ValueError，经
    AgentRunTool 转 E_TOOL_FAIL 小票——不造空锚把 worker 的 findings 写飞。
    """
    db_path = os.environ.get("SECPT_DB", "secpt.db")
    if db_path == ":memory:":
        raise SecptError(
            "多 worker 需要文件库（SECPT_DB 不能是 :memory:）",
            code="E_CONFIG",
            context={"db": db_path},
        )
    size_raw = os.environ.get("SECPT_WORKERS", str(_DEFAULT_WORKERS)).strip() or str(
        _DEFAULT_WORKERS
    )
    try:
        size = int(size_raw)
    except ValueError as exc:
        raise SecptError(
            "SECPT_WORKERS 必须是整数",
            code="E_CONFIG",
            context={"got": size_raw},
        ) from exc
    if size <= 0:
        raise SecptError("SECPT_WORKERS 必须为正整数", code="E_CONFIG", context={"got": size})
    return MissionBoard(
        conn,
        _worker_builder(),
        size=size,
        task_resolver=lambda c: ledger.latest_task(c).id,
    )


def _open_db():
    """事实层唯一入口：env 指路，migrate 保证 schema 幂等推进。"""
    return connect(os.environ.get("SECPT_DB", "secpt.db"))


def _registry(
    *, board: MissionBoard | None = None, runner: ArgvRunner | None = None
) -> ToolRegistry:
    """工具货架：nmap（外部适配器）+ agent_run / declare_finding（内嵌）；
    M3-1 起 Guard 从 profile.toml [guard] 段装配——缺段/缺文件为 no-op
    （闸常开，与 M1/M2 行为逐字节一致），坏 tier 值才 E_PROFILE 拒跑。
    M3-2 起 HITL：prompt 档=CliApprover（stdin 阻塞提问，蓝本 v0.1 形态），
    其余档（含缺省 auto）装配非交互审批人；off 档审批人闲置（闸内已拦死）。
    M3-3 起探针：probe=None = 不开窗（R6 关档零开销）；开档按
    SECPT_PROBE_BACKEND 装配事件源（默认 mock 伪事件源）。
    M3-5 起双注入：runner=执行隔离（None=本机），board=领单板（None=占位）。"""
    raw_dir = os.environ.get("SECPT_RAW_DIR", "raw")
    binary = os.environ.get("SECPT_NMAP_BIN", "/usr/bin/nmap")
    guard = load_guard_config(_profile_path())
    approver: Approver | None = None
    if guard is not None:
        approver = CliApprover() if guard.hitl_mode == "prompt" else AutoApprover()
    probe = _probe_backend()
    return ToolRegistry(
        [
            NmapTool(binary=binary, raw_dir=raw_dir, runner=runner),
            AgentRunTool(board=board),
            DeclareFindingTool(),
            *_mcp_adapters(),
        ],
        guard=guard,
        approver=approver,
        probe=probe,
    )


def _probe_backend_name() -> str:
    """探针后端选型键（M4-3）：SECPT_PROBE_BACKEND，默认 mock。

    note_for 与 build_backend 各取一次但同源同值（纯读 env），装配注记与
    实际后端永不串账——账面说 mitmproxy 就真是 mitmproxy。
    """
    return os.environ.get("SECPT_PROBE_BACKEND", "mock").strip() or "mock"


def _probe_backend() -> CaptureBackend | None:
    """M3-3 探针后端装配：开档选型（默认 mock），关档 None = 零开销。

    load_probe_config 对同一份 profile.toml 幂等（纯读、无状态），装配注记
    （probe_note，_assemble 里另行取）与后端虽各读一次，口径恒一致不串账。
    M4-3 起 mitmproxy 档 = 真抓包（装配期 pre-flight fail-closed，见 probe.py）。
    """
    cfg = load_probe_config(_profile_path())
    if not cfg.enabled:
        return None
    return build_backend(_probe_backend_name(), os.environ.get("SECPT_RAW_DIR", "raw"))


def _mcp_adapters() -> list[ToolAdapter]:
    """MCP 管道装配（M4-2）：profile [mcp] 段 → adapter 列表。

    掉线只告警不注册（build_mcp_adapters 捕 McpError 转 structlog warning），
    装配绝不被坏 server 炸；未配置 [mcp] 段 = 空列表，零行为变化。
    """
    return build_mcp_adapters(
        load_mcp_config(_profile_path()), os.environ.get("SECPT_RAW_DIR", "raw")
    )


def _experience_env() -> str:
    """经验库环境键（W3-1 拍板）：SECPT_ENV 环境变量，默认 lab。

    为什么用环境键而不是配置文件：回放的隔离粒度是"环境相同才回流"，
    演示与测试各指各的环境互不串账，一个 env 足够，不必引入配置层。
    """
    env = os.environ.get("SECPT_ENV", "lab").strip()
    return env or "lab"


def _profile_path() -> str:
    """裁判 profile 装配路径（拍板③）：SECPT_PROFILE 指路，默认 configs/profile.toml。"""
    return os.environ.get("SECPT_PROFILE", "configs/profile.toml")


def _assemble(
    conn: sqlite3.Connection,
    client: LlmClient,
    *,
    board: MissionBoard | None = None,
    runner: ArgvRunner | None = None,
) -> PentestLoop:
    """主循环装配：事件总线挂 CliView（对话面板），dispatcher 走 registry，
    出场三验由 ExitChecklist 实现依赖倒置口（core 不 import finalize）；
    W3-1 起收尾挂经验蒸馏器，W3-2 起裁判 sweep 以 partial 注入 JudgeSweep 口
    （core 只见 callable；profile 缺文件由 load_profile 降关闭 + note 携带降级口径），
    全部与 MainLlm 共用同一条 chat 协议 client。
    M3-5：board/runner 由调用方持有并负责收板（装配层不管生命周期）。"""
    bus = EventBus()
    bus.subscribe(CliView())
    prof = load_profile(_profile_path())
    probe_cfg = load_probe_config(_profile_path())
    reg = _registry(board=board, runner=runner)
    # M3-4 判票接线（拍板①注入式）：judge 只见 callable 与环境键，重放怎么跑
    # （只读门/Guard 同权）是 registry 的事；environment=None 才会票盒静默。
    return PentestLoop(
        conn,
        MainLlm(client),
        reg,
        bus=bus,
        exit_checker=ExitChecklist(),
        distiller=ExperienceDistiller(client),
        experience_env=_experience_env(),
        judge_sweep=partial(
            sweep,
            profile=prof,
            replay=reg.readonly_replay,
            environment=_experience_env(),
        ),
        judge_note=note_for(prof),
        coach=Coach(client),
        probe_note=probe_note_for(probe_cfg, _probe_backend_name()),
    )


def _emit_report(
    conn: sqlite3.Connection, provider: LlmProvider, task_id: str, *, half: bool
) -> None:
    """LLMA 报告落 stdout（JSON + Markdown）；报告侧失败只 stderr 降级。

    为什么吞在这里而不上抛：报告是呈现层，失败绝不能掩盖 run 的成败结论
    （正常路径）或原异常（半场路径）——兜底边界，仅此一处允许宽捕获。
    """
    try:
        rep = Reporter(LlmClient(provider)).generate(conn, task_id, half_court=half)
        print(json.dumps(rep.model_dump(), ensure_ascii=False))
        print(render_markdown(rep))
    except Exception as exc:  # noqa: BLE001  呈现层兜底：报告失败降级为告警
        print(
            json.dumps({"event": "report_degraded", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )


def _half_report(
    conn: sqlite3.Connection, task_id: str | None, provider: LlmProvider | None
) -> None:
    """半场报告兜底（WF7 硬性动作）：SecptError 出口后必走；报告失败只告警降级。

    task_id 为空（异常早于 task 创建）时取最近 task 取材——半场报告是异常出口的
    机构定局动作，宁可空转也要落到 audit 存在过这步，不静默跳过。provider 为
    None（装配本身失败）时退回出厂脚本：此时没有可用的真模型，如实标注 mock。
    """
    try:
        if task_id is None:
            task_id = ledger.latest_task(conn).id
        if provider is None:
            provider = FakeLLM(
                [
                    json.dumps(
                        {"summary": "（mock 半场转述）异常出口收束：已落账部分如实报告。"},
                        ensure_ascii=False,
                    )
                ]
            )
        _emit_report(conn, provider, task_id, half=True)
    except Exception as exc:  # noqa: BLE001  兜底层同理：不覆盖将被 re-raise 的原异常
        print(
            json.dumps({"event": "half_report_degraded", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )


def _cmd_run(args: argparse.Namespace) -> int:
    """``secpt run --goal ...``：WF1 有 goal → 进场三问 → 主循环 → 终场报告。"""
    goal = str(args.goal).strip()
    if not goal:
        # WF1 失败路径：无 goal → 对话模式兜底，不开闭环（不悬死）。
        print("goal 为空 → 对话模式兜底（WF1）：不进闭环；需要对话请用 secpt chat。")
        return 0
    conn = _open_db()
    board: MissionBoard | None = None
    provider: LlmProvider | None = None
    try:
        provider = _main_provider(goal)  # 装配期校验在进场前（E_CONFIG 不留半场烂账）
        client = LlmClient(provider)  # main LLM 与蒸馏器共存同一条协议通道（W3-1）
        board = _board(conn)
        result = _assemble(conn, client, board=board, runner=_runner()).run(goal)
        _print_result(result)
        _emit_report(conn, provider, result.task_id, half=False)
    except SecptError:
        _half_report(conn, None, provider)
        raise
    finally:
        if board is not None:
            board.shutdown()
        conn.close()
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    """``secpt resume <task_id>``：WF8 只复建不回放，从水位线+1 续走。"""
    conn = _open_db()
    board: MissionBoard | None = None
    provider: LlmProvider | None = None
    try:
        provider = _main_provider("")
        board = _board(conn)
        result = _assemble(conn, client=LlmClient(provider), board=board, runner=_runner()).resume(
            str(args.task_id)
        )
        _print_result(result)
        _emit_report(conn, provider, result.task_id, half=False)
    except SecptError:
        _half_report(conn, str(args.task_id), provider)
        raise
    finally:
        if board is not None:
            board.shutdown()
        conn.close()
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    """``secpt chat [text]``：对话模式单轮直答（§8 可观测流起点）。"""
    text = str(args.text) if args.text else sys.stdin.readline().rstrip("\n")
    if not text.strip():
        print('（chat）空输入。用法：secpt chat "你好"（或管道喂入）。')
        return 0
    reply = _chat_provider().complete(
        [Message(role="user", content=text)], temperature=TEMPERATURE_FREESTYLE, timeout_s=60.0
    )
    print(f"【AI】{reply}")
    return 0


def _cmd_tools(args: argparse.Namespace) -> None:
    """``secpt tools``：货架清单（manifest+hash，M3 Guard 的比对锚同源）。"""
    registry = _registry()
    for manifest in registry.manifests():
        params = ",".join(f"{k}:{v}" for k, v in manifest.params.items())
        required = ",".join(manifest.required)
        tier = registry.tier_of(manifest.name)
        tier_part = f"  tier={tier}" if tier else ""
        print(f"{manifest.name}  kind={manifest.kind}{tier_part}")
        print(f"    desc={manifest.description}")
        print(f"    params={params}  required={required}")
        print(f"    hash={manifest.hash}")
    return 0


def _cmd_evidence(args: argparse.Namespace) -> int:
    """``secpt evidence <task_id>``：账本落地检查窗（只读回放）。"""
    conn = _open_db()
    try:
        lines = evidence_report(conn, str(args.task_id))
    finally:
        conn.close()
    print("\n".join(lines))
    return 0


def _cmd_panel(args: argparse.Namespace) -> int:
    """``secpt panel``：只读 web 面板（账本/双图/报告/audit 流，无写路径）。

    为什么只连 GET-only 的 Flask app：面板与引擎仅共享账本文件，ro 连接打开——
    服务永远不会写账本；threaded=True 只为 SSE 长连接不阻塞其它页面请求，
    引擎侧"M2 不做并发"约束不涉此处（§66 记档口径）。
    """
    app = create_app(_open_db_path())
    app.run(
        host=str(args.host), port=int(args.port), threaded=True, debug=False, use_reloader=False
    )
    return 0


def _open_db_path() -> str:
    """面板专用的账本路径读取（不开连接：create_app 内部各自 ro 连接）。"""
    return os.environ.get("SECPT_DB", "secpt.db")


def _print_result(result: LoopResult) -> None:
    """收束摘要：身份 + 步数 + 出口类型（CLI 面板的最后一行）。"""
    print(f"== run 收束 == task_id={result.task_id} run_id={result.run_id}")
    print(
        f"   steps={result.steps_taken} exit_kind={result.exit_kind}"
        f" finished={result.finished_naturally}"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="secpt", description="渗透测试智能体 harness（M1：LLM 聪明、骨架不犯低级错误）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="渗透任务模式（进场三问 → 主循环）")
    p_run.add_argument("--goal", required=True, help="任务目标描述")
    p_run.set_defaults(func=_cmd_run)

    p_chat = sub.add_parser("chat", help="对话模式（单轮直答，不进闭环）")
    p_chat.add_argument("text", nargs="?", default=None, help="一句话；缺省读 stdin")
    p_chat.set_defaults(func=_cmd_chat)

    p_resume = sub.add_parser("resume", help="断电续跑（WF8：只复建不回放）")
    p_resume.add_argument("task_id", help="要续跑的 task")
    p_resume.set_defaults(func=_cmd_resume)

    p_tools = sub.add_parser("tools", help="列工具货架（manifest+hash）")
    p_tools.set_defaults(func=_cmd_tools)

    p_evidence = sub.add_parser("evidence", help="账本落地检查窗（小票/双图）")
    p_evidence.add_argument("task_id", help="要检查的 task")
    p_evidence.set_defaults(func=_cmd_evidence)

    p_panel = sub.add_parser("panel", help="只读 web 面板（账本/双图/报告/audit 流）")
    p_panel.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    p_panel.add_argument("--port", type=int, default=8711, help="监听端口")
    p_panel.set_defaults(func=_cmd_panel)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 兜底出口：库层不吞错统一上抛到这里，转结构化错误 + 非零退出。"""
    configure_logging()
    try:
        args = _build_parser().parse_args(argv)
        rc = int(args.func(args))
    except SecptError as exc:
        print(
            json.dumps(
                {"event": "abnormal_exit", **exc.to_dict()}, ensure_ascii=False, default=str
            ),
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("中断：未收尾的 run 可用 secpt resume <task_id> 续跑", file=sys.stderr)
        return 130
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
