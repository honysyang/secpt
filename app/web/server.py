"""Web 前端服务：FastAPI + SSE 流式对话。

复用 Harness 现有全部运行链路（LLM 韧性 / HITL 审批 / 工具 / 角色注册表），
三种模式与 CLI 对齐：
- single: ReAct 循环 + 跨轮会话历史 + 流式 token
- team:   Router 路由（发路由进度事件）
- swarm:  事件驱动集群（发意图认领/完成进度事件）

启动: python -m uvicorn app.web.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import contextvars
import json
import os
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.bootstrap import build_tool_registry, create_llm, load_roles

app = FastAPI(title="secpt Web")

_LOCAL_HOSTS = {"127.0.0.1", "::1", "testclient"}  # testclient: FastAPI 测试


@app.middleware("http")
async def auth_middleware(request: Request, call_next: Any) -> Any:
    """API 认证：配置了 WEB_API_KEY 时强制校验 X-Api-Key；
    未配置时仅放行本机来源，防止配置中心/对话接口暴露成远程控制面。"""
    if request.url.path.startswith("/api") and request.url.path != "/api/health":
        expected = os.getenv("WEB_API_KEY", "")
        client_host = request.client.host if request.client else ""
        if expected:
            if request.headers.get("X-Api-Key") != expected:
                return JSONResponse(
                    {"error": "未授权：缺少或错误的 X-Api-Key"}, status_code=401
                )
        elif client_host not in _LOCAL_HOSTS:
            return JSONResponse(
                {
                    "error": "未授权：远程访问需在 .env 配置 WEB_API_KEY 并在请求头携带 X-Api-Key"
                },
                status_code=401,
            )
    return await call_next(request)

_STATIC_DIR = Path(__file__).parent / "static"
_APPROVAL_TIMEOUT = 300.0  # 危险工具审批等待上限（秒）


# ---------------------------------------------------------------------------
# 应用级单例（进程内共享；工具/角色/LLM 构造成本高，启动时一次）
# ---------------------------------------------------------------------------

@dataclass
class WebSession:
    """一个浏览器会话：消息历史 + 所属模式。"""

    messages: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "single"


_sessions: OrderedDict[str, WebSession] = OrderedDict()
_pending_approvals: dict[str, asyncio.Future[bool]] = {}
_MAX_SESSIONS = 50  # LRU 上限：防止长驻进程会话数无限增长
_SESSIONS_SNAPSHOT = Path("snapshots/web_sessions.json")


def _touch_session(session_id: str) -> WebSession:
    """取会话并移到 LRU 队尾；超限时逐出最旧会话。"""
    session = _sessions.get(session_id)
    if session is None:
        session = WebSession()
        _sessions[session_id] = session
    _sessions.move_to_end(session_id)
    while len(_sessions) > _MAX_SESSIONS:
        _sessions.popitem(last=False)
    return session


def _persist_sessions() -> None:
    """会话历史落盘（静默降级：失败只影响重启后的历史恢复）。"""
    try:
        _SESSIONS_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        data = {
            sid: {"messages": s.messages, "mode": s.mode}
            for sid, s in _sessions.items()
        }
        _SESSIONS_SNAPSHOT.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


def _restore_sessions() -> None:
    """启动时恢复上次运行的历史会话（文件缺失/损坏则跳过）。"""
    try:
        if not _SESSIONS_SNAPSHOT.exists():
            return
        data = json.loads(_SESSIONS_SNAPSHOT.read_text(encoding="utf-8"))
        for sid, item in data.items():
            _sessions[sid] = WebSession(
                messages=item.get("messages", []), mode=item.get("mode", "single")
            )
    except Exception:
        pass


_restore_sessions()
atexit.register(_persist_sessions)


def _get_components() -> dict[str, Any]:
    """惰性初始化并缓存运行组件（首个请求时构造，避免 import 副作用）。"""
    global _components
    if _components is None:
        from runtime.agents.collaboration import Router
        from runtime.event_driven import EventDrivenEngine
        from runtime.loop import ExecutionLoop

        llm = _build_llm()
        tools = _build_tools()
        roles = load_roles()
        experts = roles.create_experts(llm, shared_tools=tools)
        coordinator = roles.create_agent("coordinator", llm)
        _components = {
            "llm": llm,
            "tools": tools,
            "roles": roles,
            "coordinator": coordinator,
            "experts": experts,
            "loop_cls": ExecutionLoop,
            "router_cls": Router,
            "engine_cls": EventDrivenEngine,
        }
    return _components


def _build_llm() -> Any:
    """LLM 构造（共享引导模块，含 .env 加载与 ResilientLLM 韧性包装）。"""
    return create_llm()


def _build_tools() -> Any:
    """工具注册表（共享引导模块）+ Web 常驻审批钩子。

    钩子内部经 contextvars 路由到当前请求的队列——并发请求互不串扰，
    也无需在请求间修改全局状态。
    """
    registry = build_tool_registry(load_script_tools=True)
    registry.approval_hook = _web_approval_hook()
    return registry


_components: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    """对话请求。"""

    session_id: str
    message: str
    mode: str = "single"


class ApprovalRequest(BaseModel):
    """HITL 审批决定。"""

    approved: bool


class RolePayload(BaseModel):
    """角色配置写入载荷（instructions 必填，缺失时 422）。"""

    description: str = ""
    instructions: str
    tools: list[str] = field(default_factory=list)
    max_steps: int = 10


# ---------------------------------------------------------------------------
# 核心执行器：把 Harness 执行过程翻译为 SSE 事件序列
# ---------------------------------------------------------------------------

async def _emit(queue: asyncio.Queue[dict[str, Any]], **event: Any) -> None:
    """向事件队列投递一条 SSE 事件。"""
    await queue.put(event)


def _format_loop_event(event: dict[str, Any]) -> str:
    """把 ExecutionLoop 过程事件压缩为一行可读状态。"""
    kind = event.get("type")
    if kind == "tool_call":
        args = str(event.get("args", ""))[:60]
        return f"🔧 调用工具 {event.get('tool')}({args})"
    if kind == "tool_result":
        first = next(
            (l for l in str(event.get("content", "")).splitlines() if l.strip()), ""
        )
        return f"👀 {first[:80]}"
    if kind == "tool_rejected":
        return f"✗ 用户拒绝执行 {event.get('tool')}"
    return str(kind)


async def _run_single(req: ChatRequest, session: WebSession, queue: asyncio.Queue) -> dict[str, Any]:
    """单智能体：流式 ReAct（on_token 是 ExecutionLoop 的构造参数）。"""
    comp = _get_components()
    loop = comp["loop_cls"](
        llm=comp["llm"],
        tools=comp["tools"],
        max_steps=15,
        on_token=lambda t: queue.put_nowait({"type": "token", "content": t}),
        on_event=lambda ev: queue.put_nowait(
            {"type": "status", "content": _format_loop_event(ev)}
        ),
    )
    result = await loop.run_react(req.message, history=session.messages)
    session.messages = result.messages or session.messages
    return {"type": "done", "tokens": result.total_tokens, "steps": len(result.steps)}


async def _run_team(req: ChatRequest, queue: asyncio.Queue) -> dict[str, Any]:
    """多智能体路由：发路由进度事件，专家回复流式输出。"""
    comp = _get_components()
    router = comp["router_cls"](coordinator=comp["coordinator"], sub_agents=comp["experts"])

    await _emit(queue, type="status", content="协调者正在分析任务并选择专家…")
    result = await router.route(
        req.message,
        on_token=lambda t: queue.put_nowait({"type": "token", "content": t}),
    )
    routed = result.metadata.get("routed_to", "unknown")
    await _emit(queue, type="status", content=f"路由到专家: {routed}")

    for agent_result in result.agent_results:
        await _emit(
            queue,
            type="status",
            content=f"{agent_result.agent_name} 完成 ({agent_result.tokens_used} tokens)",
        )
    # 全文已通过 on_token 流式推送，不再整体重发（前端会重复渲染）
    return {"type": "done", "tokens": sum(r.tokens_used for r in result.agent_results), "steps": 0}


async def _run_swarm(req: ChatRequest, queue: asyncio.Queue) -> dict[str, Any]:
    """事件驱动集群：发意图认领/完成进度事件。"""
    comp = _get_components()
    blackboard = _swarm_blackboard

    async def execute_intent(intent: Any) -> str | None:
        agent = comp["experts"].get(intent.agent_role)
        if agent is None:
            return None
        await _emit(queue, type="status", content=f"▶ [{intent.agent_role}] {intent.description}")

        def push_token(text: str) -> None:
            # agent 标记：前端据此在 Worker 产出变化处插分隔行，避免多 Worker 文本混排
            queue.put_nowait({"type": "token", "content": text, "agent": intent.agent_role})

        result = await agent.run(intent.description, on_token=push_token)
        try:
            await blackboard.write_domain("tasks", f"intent_{intent.id}", result.output, agent="web")
        except Exception:
            pass
        await _emit(
            queue,
            type="status",
            content=f"✔ [{intent.agent_role}] 完成 ({result.tokens_used} tokens)",
        )
        return result.output

    engine = comp["engine_cls"](
        blackboard,
        comp["llm"],
        num_workers=2,
        executor=execute_intent,
        available_roles=list(comp["experts"].keys()),
    )
    report = await engine.run(task_description=req.message)

    # 汇总各意图结果为最终回答
    sections: list[str] = []
    for intent in report["intents"]:
        if intent.result:
            first_line = str(intent.result).strip().split("\n", 1)[0][:80]
            sections.append(f"- **[{intent.agent_role}]** {intent.description} → {first_line}")
    summary = "### 各意图执行摘要\n\n" + "\n".join(sections) if sections else "（本次没有产生执行结果）"
    await _emit(queue, type="token", content=summary)
    return {"type": "done", "tokens": 0, "steps": 0}


_swarm_blackboard: HierarchicalBlackboard | None = None


# 当前请求的 SSE 队列（审批事件路由用）：Task 启动时继承上下文快照，
# 并发请求天然隔离——修复"共享 approval_hook 被并发请求互相覆盖"的竞态
_current_approval_queue: contextvars.ContextVar[asyncio.Queue | None] = (
    contextvars.ContextVar("approval_queue", default=None)
)


def _web_approval_hook() -> Any:
    """Web 端 HITL：审批请求经 SSE 推给当前请求的前端弹窗，等待用户决定。

    钩子常驻注册表；请求上下文经 contextvars 注入，并发安全。
    请求上下文之外调用（如误挂到 CLI 链路）一律拒绝执行，宁可保守。
    """

    async def approve(tool_name: str, arguments: str) -> bool:
        queue = _current_approval_queue.get()
        if queue is None:
            return False
        approval_id = uuid.uuid4().hex
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        _pending_approvals[approval_id] = fut
        try:
            await _emit(
                queue,
                type="approval_request",
                approval_id=approval_id,
                tool=tool_name,
                args=arguments[:300],
            )
            return await asyncio.wait_for(fut, timeout=_APPROVAL_TIMEOUT)
        finally:
            _pending_approvals.pop(approval_id, None)

    return approve


# ---------------------------------------------------------------------------
# 配置中心：角色（读 + 写 + 热重载）/ 工具（只读）/ 技能（只读）
# ---------------------------------------------------------------------------

_ROLES_JSON = Path("configs/roles.json")


def _read_json_roles() -> dict[str, dict[str, Any]]:
    """读取 roles.json 的自定义/覆盖角色段（文件缺失/损坏时降级为空）。"""
    try:
        data = json.loads(_ROLES_JSON.read_text(encoding="utf-8"))
        roles = data.get("roles")
        return roles if isinstance(roles, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json_roles(roles: dict[str, dict[str, Any]]) -> None:
    """写回 roles.json（保持 {"roles": {...}} 的 CLI 加载格式）。"""
    _ROLES_JSON.parent.mkdir(parents=True, exist_ok=True)
    _ROLES_JSON.write_text(
        json.dumps({"roles": roles}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _reload_components() -> None:
    """热重载角色配置：复用现有 llm/tools 实例，重建 roles/experts/coordinator。

    swarm 的 engine 每请求新建、team 的 router 每请求新建，
    替换 _components 后下一次对话自动生效。
    """
    global _components
    comp = _get_components()
    roles = load_roles(path=_ROLES_JSON)
    _components = {
        **comp,
        "roles": roles,
        "coordinator": roles.create_agent("coordinator", comp["llm"]),
        "experts": roles.create_experts(comp["llm"], shared_tools=comp["tools"]),
    }


def _role_source(name: str, json_roles: dict[str, Any]) -> str:
    """角色来源：builtin 内置 / custom 自定义 / override 覆盖内置。"""
    if name not in json_roles:
        return "builtin"
    from runtime.agents.roles import BUILTIN_ROLES

    builtin_names = {r.name for r in BUILTIN_ROLES}
    return "override" if name in builtin_names else "custom"


@app.get("/api/config/roles")
async def config_list_roles() -> JSONResponse:
    """全部角色（内置 + JSON 自定义/覆盖），含完整定义与来源标记。"""
    specs = _get_components()["roles"].list_specs()
    json_roles = _read_json_roles()
    items = [
        {
            "name": s.name,
            "description": s.description,
            "instructions": s.instructions,
            "tools": s.tools,
            "max_steps": s.max_steps,
            "source": _role_source(s.name, json_roles),
        }
        for s in specs.values()
    ]
    return JSONResponse(items)


@app.post("/api/config/roles/{name}")
async def config_save_role(name: str, payload: RolePayload) -> JSONResponse:
    """新增/覆盖角色：校验 → 写 roles.json → 热重载立即生效。"""
    from runtime.agents.roles import RoleError, RoleSpec

    data = {
        "instructions": payload.instructions,
        "description": payload.description,
        "tools": payload.tools,
        "max_steps": payload.max_steps,
    }
    try:
        RoleSpec.from_dict(name, data)  # 写文件前先校验，避免落盘坏配置
    except RoleError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not payload.instructions.strip():
        return JSONResponse({"error": "instructions 不能为空白"}, status_code=400)

    json_roles = _read_json_roles()
    json_roles[name] = data
    _write_json_roles(json_roles)
    _reload_components()
    return JSONResponse({"ok": True, "name": name, "source": _role_source(name, json_roles)})


@app.delete("/api/config/roles/{name}")
async def config_delete_role(name: str) -> JSONResponse:
    """删除角色：移除 JSON 中的自定义/覆盖定义；纯内置角色不可删。"""
    json_roles = _read_json_roles()
    if name not in json_roles:
        return JSONResponse(
            {"error": f"角色 {name!r} 不在配置文件中（内置角色不可删除）"},
            status_code=404,
        )
    del json_roles[name]
    _write_json_roles(json_roles)
    _reload_components()
    return JSONResponse({"ok": True, "name": name})


@app.get("/api/config/tools")
async def config_list_tools() -> JSONResponse:
    """全部已注册工具（含参数 Schema 与危险标记），只读。"""
    items = [
        {
            "name": t.name,
            "description": t.description,
            "dangerous": t.dangerous,
            "parameters": t.parameters,
        }
        for t in _get_components()["tools"].list_items()
    ]
    return JSONResponse(items)


@app.get("/api/config/skills")
async def config_list_skills() -> JSONResponse:
    """已注册技能清单。当前业务链路尚无技能注册，返回空列表。"""
    return JSONResponse([])


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.post("/api/chat")
async def chat(req: ChatRequest) -> JSONResponse:
    """SSE 对话端点：逐事件推送 token/status/approval/done。"""
    global _swarm_blackboard

    if not req.message.strip():
        return JSONResponse({"error": "消息为空"}, status_code=400)

    session = _touch_session(req.session_id)
    session.mode = req.mode
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # swarm 模式使用进程级共享黑板（跨会话累积发现，与 CLI 行为一致）
    if req.mode == "swarm" and _swarm_blackboard is None:
        from runtime.blackboard import HierarchicalBlackboard

        _swarm_blackboard = HierarchicalBlackboard()

    # 审批上下文注入：Task 启动时继承快照，并发请求互不串扰（P0 修复）
    approval_ctx = _current_approval_queue.set(queue)

    async def run() -> None:
        try:
            if req.mode == "team":
                meta = await _run_team(req, queue)
            elif req.mode == "swarm":
                meta = await _run_swarm(req, queue)
            else:
                meta = await _run_single(req, session, queue)
            await _emit(queue, **meta)
        except Exception as exc:  # noqa: BLE001 - SSE 边界需兜底
            await _emit(queue, type="error", message=str(exc))
        finally:
            _current_approval_queue.reset(approval_ctx)
            _persist_sessions()

    task = asyncio.create_task(run())

    async def sse_gen():
        """手写 SSE 帧（data: {json}\\n\\n），避免 sse_starlette 的模块级
        AppStatus 在多请求/多事件循环环境下绑定冲突。"""
        try:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event["type"] in ("done", "error"):
                    break
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    from fastapi.responses import StreamingResponse

    return StreamingResponse(sse_gen(), media_type="text/event-stream")


@app.post("/api/approval/{approval_id}")
async def approval(approval_id: str, body: ApprovalRequest) -> JSONResponse:
    """前端 HITL 审批回调。"""
    fut = _pending_approvals.get(approval_id)
    if fut is None or fut.done():
        return JSONResponse({"error": "审批请求不存在或已过期"}, status_code=404)
    fut.set_result(body.approved)
    return JSONResponse({"ok": True})


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "sessions": len(_sessions)})


# 静态文件：首页 + 资源（放最后，避免吞掉 API 路由）
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    # no-cache: 每次导航都 revalidate（ETag 协商），避免迭代时浏览器用旧版页面
    return FileResponse(_STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


def main() -> None:
    """直接运行的入口。

    绑定策略与认证联动：未配置 WEB_API_KEY 时只绑本机回环；
    配置后允许对外监听（API 仍强制校验 X-Api-Key）。
    """
    import uvicorn

    host = "0.0.0.0" if os.getenv("WEB_API_KEY") else "127.0.0.1"
    uvicorn.run(app, host=host, port=8000, log_level="info")


if __name__ == "__main__":
    main()
