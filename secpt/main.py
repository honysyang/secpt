"""CLI 唯一入口（§6.3 五命令）：secpt run / chat / resume / tools / evidence。

装配与兜底纪律：
- main() 是库层异常的第一兜底（§9：库层不吞错、统一上抛）——SecptError 落一行
  结构化 JSON 进 stderr 后非零退出，绝不带 traceback、绝不让循环悬死。
- provider 装配（诚实边界）：M1 只接 ``LLM_PROVIDER=mock``（FakeLLM 出厂演示
  脚本，离线可全流程演示）；其余值装配期显式拒绝——真实 provider 是 M2 接线
  件，M1 不装也不假装。
- 环境变量：``SECPT_DB``（默认 ./secpt.db）、``SECPT_RAW_DIR``（默认 ./raw）、
  ``SECPT_NMAP_BIN``（默认 /usr/bin/nmap；演示环境可指向占位路径强制回执）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from secpt.core.loop import LoopResult, PentestLoop
from secpt.errors import SecptError
from secpt.llms.client import TEMPERATURE_FREESTYLE, LlmClient, Message, StepOutput, ToolCall
from secpt.llms.fake import FakeLLM
from secpt.llms.main_llm import MainLlm
from secpt.ob.cli_view import CliView, configure_logging, evidence_report
from secpt.ob.events import EventBus
from secpt.store.migrate import connect
from secpt.tools.adapters import AgentRunTool, NmapTool
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
    """M1 出厂演示脚本：进场三问 → 一步 nmap 摸扫 → 一步收工（done）。

    脚本内容与 goal 文本无关——FakeLLM 不真理解输入，target 取输入截断只是
    让账本可读；nmap 由 env 指向占位二进制必落 E_TOOL_FAIL 回执，整程零网络。
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
    return FakeLLM([goal_json, sweep, wrap_up])


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
    """M1 工具货架：nmap（外部适配器）+ agent_run（内嵌登记占位）。"""
    raw_dir = os.environ.get("SECPT_RAW_DIR", "raw")
    binary = os.environ.get("SECPT_NMAP_BIN", "/usr/bin/nmap")
    return ToolRegistry([NmapTool(binary=binary, raw_dir=raw_dir), AgentRunTool()])


def _assemble(conn, llm: MainLlm) -> PentestLoop:
    """主循环装配：事件总线挂上 CliView（对话面板），dispatcher 走 registry。"""
    bus = EventBus()
    bus.subscribe(CliView())
    return PentestLoop(conn, llm, _registry(), bus)


def _cmd_run(args: argparse.Namespace) -> int:
    """``secpt run --goal ...``：WF1 有 goal → 进场三问 → 主循环。"""
    goal = str(args.goal).strip()
    if not goal:
        # WF1 失败路径：无 goal → 对话模式兜底，不开闭环（不悬死）。
        print("goal 为空 → 对话模式兜底（WF1）：不进闭环；需要对话请用 secpt chat。")
        return 0
    llm = MainLlm(LlmClient(_demo_provider(goal)))  # provider 校验在进场前
    conn = _open_db()
    try:
        result = _assemble(conn, llm).run(goal)
    finally:
        conn.close()
    _print_result(result)
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    """``secpt resume <task_id>``：WF8 只复建不回放，从水位线+1 续走。"""
    conn = _open_db()
    try:
        llm = MainLlm(LlmClient(_demo_provider("")))
        result = _assemble(conn, llm).resume(str(args.task_id))
    finally:
        conn.close()
    _print_result(result)
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
