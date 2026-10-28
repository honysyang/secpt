"""CLI 唯一入口（§6.3 五命令）：secpt run / chat / resume / tools / evidence。

装配与兜底纪律：
- main() 是库层异常的第一兜底（§9：库层不吞错、统一上抛）——SecptError 落一行
  结构化 JSON 进 stderr 后非零退出，绝不带 traceback、绝不让循环悬死。
- provider 装配（诚实边界）：M1 只接 ``LLM_PROVIDER=mock``（FakeLLM 出厂演示
  脚本，离线可全流程演示）；其余值装配期显式拒绝——真实 provider 是 M2 接线
  件，M1 不装也不假装。
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
from collections.abc import Sequence
from functools import partial

from secpt.core.loop import LoopResult, PentestLoop
from secpt.errors import SecptError
from secpt.finalize.exit_checklist import ExitChecklist
from secpt.finalize.experience import Distiller as ExperienceDistiller
from secpt.finalize.reporter import Reporter, render_markdown
from secpt.judge import load_profile, note_for, sweep
from secpt.llms.client import TEMPERATURE_FREESTYLE, LlmClient, Message, StepOutput, ToolCall
from secpt.llms.fake import FakeLLM
from secpt.llms.main_llm import MainLlm
from secpt.ob.cli_view import CliView, configure_logging, evidence_report
from secpt.ob.events import EventBus
from secpt.store import ledger
from secpt.store.migrate import connect
from secpt.tools.adapters import AgentRunTool, DeclareFindingTool, NmapTool
from secpt.tools.registry import ToolRegistry

_NULL_TARGET = "(demo)"


def _require_mock() -> None:
    """provider 装配校验：M1 只有 mock 一条腿，其余值显式拒绝（诚实边界）。"""
    name = os.environ.get("LLM_PROVIDER", "mock")
    if name != "mock":
        raise SecptError(
            "M1 仅支持 LLM_PROVIDER=mock（真实 provider 是 M2 的接线件）",
            context={"provider": name},
        )


def _demo_provider(goal_text: str) -> FakeLLM:
    """M1 出厂演示脚本：进场三问 → 一步 nmap 摸扫 → 一步收工（done）→ 报告摘要。

    脚本内容与 goal 文本无关——FakeLLM 不真理解输入，target 取输入截断只是
    让账本可读；nmap 由 env 指向占位二进制必落 E_TOOL_FAIL 回执，整程零网络。
    尾项是 LLMA 报告的 summary 脚本（run 正常收束后由 _emit_report 消费）；
    resume 不跑进场（无 goal 项），直接收工 + 摘要。
    """
    _require_mock()
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
    if goal_text:
        return FakeLLM([goal_json, sweep, wrap_up, summary])
    return FakeLLM([wrap_up, summary])


def _chat_provider() -> FakeLLM:
    """对话模式出厂脚本：单轮直答，不进闭环、不落账（WF1）。"""
    _require_mock()
    return FakeLLM(
        [
            "（M1 mock）对话模式不进闭环：本回复由 FakeLLM 出厂脚本生成；"
            "接真实 provider 后由模型作答。"
        ]
    )


def _open_db():
    """事实层唯一入口：env 指路，migrate 保证 schema 幂等推进。"""
    return connect(os.environ.get("SECPT_DB", "secpt.db"))


def _registry() -> ToolRegistry:
    """工具货架：nmap（外部适配器）+ agent_run / declare_finding（内嵌）。"""
    raw_dir = os.environ.get("SECPT_RAW_DIR", "raw")
    binary = os.environ.get("SECPT_NMAP_BIN", "/usr/bin/nmap")
    return ToolRegistry(
        [NmapTool(binary=binary, raw_dir=raw_dir), AgentRunTool(), DeclareFindingTool()]
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


def _assemble(conn: sqlite3.Connection, client: LlmClient) -> PentestLoop:
    """主循环装配：事件总线挂 CliView（对话面板），dispatcher 走 registry，
    出场三验由 ExitChecklist 实现依赖倒置口（core 不 import finalize）；
    W3-1 起收尾挂经验蒸馏器，W3-2 起裁判 sweep 以 partial 注入 JudgeSweep 口
    （core 只见 callable；profile 缺文件由 load_profile 降关闭 + note 携带降级口径），
    全部与 MainLlm 共用同一条 chat 协议 client。"""
    bus = EventBus()
    bus.subscribe(CliView())
    prof = load_profile(_profile_path())
    return PentestLoop(
        conn,
        MainLlm(client),
        _registry(),
        bus=bus,
        exit_checker=ExitChecklist(),
        distiller=ExperienceDistiller(client),
        experience_env=_experience_env(),
        judge_sweep=partial(sweep, profile=prof),
        judge_note=note_for(prof),
    )


def _emit_report(conn: sqlite3.Connection, fake: FakeLLM, task_id: str, *, half: bool) -> None:
    """LLMA 报告落 stdout（JSON + Markdown）；报告侧失败只 stderr 降级。

    为什么吞在这里而不上抛：报告是呈现层，失败绝不能掩盖 run 的成败结论
    （正常路径）或原异常（半场路径）——兜底边界，仅此一处允许宽捕获。
    """
    try:
        rep = Reporter(LlmClient(fake)).generate(conn, task_id, half_court=half)
        print(json.dumps(rep.model_dump(), ensure_ascii=False))
        print(render_markdown(rep))
    except Exception as exc:  # noqa: BLE001  呈现层兜底：报告失败降级为告警
        print(
            json.dumps({"event": "report_degraded", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )


def _half_report(conn: sqlite3.Connection, task_id: str | None) -> None:
    """半场报告兜底（WF7 硬性动作）：SecptError 出口后必走；报告失败只告警降级。

    task_id 为空（异常早于 task 创建）时取最近 task 取材——半场报告是异常出口的
    机构定局动作，宁可空转也要落到 audit 存在过这步，不静默跳过。
    """
    try:
        if task_id is None:
            task_id = ledger.latest_task(conn).id
        fake = FakeLLM(
            [
                json.dumps(
                    {"summary": "（M1 mock 半场转述）异常出口收束：已落账部分如实报告。"},
                    ensure_ascii=False,
                )
            ]
        )
        _emit_report(conn, fake, task_id, half=True)
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
    fake = _demo_provider(goal)  # provider 校验在进场前
    client = LlmClient(fake)  # main LLM 与蒸馏器共存同一条协议通道（W3-1）
    conn = _open_db()
    try:
        result = _assemble(conn, client).run(goal)
        _print_result(result)
        _emit_report(conn, fake, result.task_id, half=False)
    except SecptError:
        _half_report(conn, None)
        raise
    finally:
        conn.close()
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    """``secpt resume <task_id>``：WF8 只复建不回放，从水位线+1 续走。"""
    conn = _open_db()
    try:
        fake = _demo_provider("")
        result = _assemble(conn, LlmClient(fake)).resume(str(args.task_id))
        _print_result(result)
        _emit_report(conn, fake, result.task_id, half=False)
    except SecptError:
        _half_report(conn, str(args.task_id))
        raise
    finally:
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
    for manifest in _registry().manifests():
        params = ",".join(f"{k}:{v}" for k, v in manifest.params.items())
        required = ",".join(manifest.required)
        print(f"{manifest.name}  kind={manifest.kind}")
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
