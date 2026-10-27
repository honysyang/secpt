"""Web 前端服务测试：静态页、健康检查、HITL 审批回调、chat SSE 链路。"""

import json

import pytest
from fastapi.testclient import TestClient

from app.web import server


class FakeLoop:
    """最小 ExecutionLoop 替身：验证 on_token 流式与 history 传递。"""

    last_history = None

    def __init__(self, llm, tools, max_steps=10, on_token=None, **kw):
        self.on_token = on_token

    async def run_react(self, task, context="", history=None):
        FakeLoop.last_history = history
        if self.on_token:
            self.on_token("你好")
            self.on_token("，世界")
        from runtime.loop import LoopResult

        return LoopResult(
            output="你好，世界", steps=[], total_tokens=5, messages=[{"role": "user", "content": task}]
        )


@pytest.fixture()
def client(monkeypatch):
    """注入替身组件，避免真实 LLM 初始化。

    只 patch _components 变量、不 patch _get_components 函数：
    真实 _get_components 读全局变量即返回替身（非 None 不走构造），
    且配置热重载对 _components 的重新绑定对测试可见。
    """
    fake = {
        "llm": object(),
        # get(): 热重载时 create_experts 解析角色工具名单用；返回 None 走"工具缺失告警跳过"的可降级路径
        "tools": type("T", (), {"approval_hook": None, "get": lambda self, name: None})(),
        "roles": None,
        "coordinator": None,
        "experts": {},
        "loop_cls": FakeLoop,
        "router_cls": None,
        "engine_cls": None,
    }
    monkeypatch.setattr(server, "_components", fake)
    return TestClient(server.app)


class TestStatic:
    def test_index_served(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "secpt 智能助理" in resp.text

    def test_health(self, client):
        assert client.get("/api/health").json()["status"] == "ok"


class TestChatSSE:
    def _parse_sse(self, resp) -> list[dict]:
        return [
            json.loads(line[5:])
            for line in resp.text.split("\n")
            if line.startswith("data:")
        ]

    def test_chat_stream_and_session_history(self, client):
        # 第一轮：流式 token + done
        events = self._parse_sse(
            client.post("/api/chat", json={"session_id": "s1", "message": "hi", "mode": "single"})
        )
        types = [e["type"] for e in events]
        assert types == ["token", "token", "done"]
        assert "".join(e.get("content", "") for e in events if e["type"] == "token") == "你好，世界"

        # 第二轮（同会话）：应携带第一轮保存的 messages 作为会话历史
        self._parse_sse(client.post("/api/chat", json={"session_id": "s1", "message": "第二轮", "mode": "single"}))
        assert FakeLoop.last_history == [{"role": "user", "content": "hi"}]

    def test_empty_message_rejected(self, client):
        resp = client.post("/api/chat", json={"session_id": "s3", "message": "  ", "mode": "single"})
        assert resp.status_code == 400


class TestApproval:
    def test_unknown_approval_404(self, client):
        resp = client.post("/api/approval/nonexistent", json={"approved": True})
        assert resp.status_code == 404


class TestConfigCenter:
    """配置中心：工具/技能只读清单 + 角色写路径（保存 → 落盘 → 热重载）。"""

    def _install_real_tools(self) -> None:
        from harness.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register("get_time", lambda: "now", description="当前时间")
        registry.register("run_command", lambda cmd: "", description="执行命令", dangerous=True)
        server._get_components()["tools"] = registry

    def test_tools_listing_with_dangerous_flag(self, client):
        self._install_real_tools()
        data = client.get("/api/config/tools").json()
        by_name = {t["name"]: t for t in data}
        assert by_name["get_time"]["dangerous"] is False
        assert by_name["run_command"]["dangerous"] is True
        assert by_name["run_command"]["description"] == "执行命令"

    def test_roles_listing_marks_source(self, client, monkeypatch):
        from runtime.agents.roles import RoleRegistry

        server._get_components()["roles"] = RoleRegistry.with_builtins()
        monkeypatch.setattr(server, "_read_json_roles", lambda: {"coder": {"instructions": "x"}})
        by_name = {r["name"]: r for r in client.get("/api/config/roles").json()}
        assert by_name["coder"]["source"] == "override"   # JSON 覆盖内置
        assert by_name["planner"]["source"] == "builtin"  # 纯内置

    def test_save_and_delete_role_roundtrip(self, client, monkeypatch, tmp_path):
        roles_json = tmp_path / "roles.json"
        roles_json.write_text('{"roles": {}}', encoding="utf-8")
        monkeypatch.setattr(server, "_ROLES_JSON", roles_json)

        # 保存：落盘 + 热重载后可查
        resp = client.post(
            "/api/config/roles/qa_tester",
            json={"description": "测试角色", "instructions": "你是测试工程师。", "tools": [], "max_steps": 8},
        )
        assert resp.json() == {"ok": True, "name": "qa_tester", "source": "custom"}
        saved = json.loads(roles_json.read_text(encoding="utf-8"))
        assert saved["roles"]["qa_tester"]["instructions"] == "你是测试工程师。"
        assert "qa_tester" in {r["name"] for r in client.get("/api/config/roles").json()}

        # 删除：再删一次应 404
        assert client.delete("/api/config/roles/qa_tester").json()["ok"] is True
        assert client.delete("/api/config/roles/qa_tester").status_code == 404

    def test_save_role_requires_instructions(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr(server, "_ROLES_JSON", tmp_path / "roles.json")
        # pydantic 只保证类型；空串由 RoleSpec 校验兜底 → 400
        resp = client.post("/api/config/roles/bad", json={"instructions": "   "})
        assert resp.status_code == 400
        assert "instructions" in resp.json()["error"]

    def test_skills_empty_but_available(self, client):
        assert client.get("/api/config/skills").json() == []


class TestAuth:
    """API 认证中间件：WEB_API_KEY 非空强制校验 X-Api-Key；为空仅放行本机。"""

    def test_missing_key_401(self, client, monkeypatch):
        monkeypatch.setenv("WEB_API_KEY", "secret123")
        resp = client.get("/api/config/skills")
        assert resp.status_code == 401
        assert "X-Api-Key" in resp.json()["error"]

    def test_wrong_key_401(self, client, monkeypatch):
        monkeypatch.setenv("WEB_API_KEY", "secret123")
        resp = client.get("/api/config/skills", headers={"X-Api-Key": "wrong"})
        assert resp.status_code == 401

    def test_correct_key_passes(self, client, monkeypatch):
        monkeypatch.setenv("WEB_API_KEY", "secret123")
        resp = client.get("/api/config/skills", headers={"X-Api-Key": "secret123"})
        assert resp.status_code == 200

    def test_health_and_index_bypass_auth(self, client, monkeypatch):
        # 存活探针与静态首页不在 /api 保护范围内
        monkeypatch.setenv("WEB_API_KEY", "secret123")
        assert client.get("/api/health").status_code == 200
        assert client.get("/").status_code == 200

    def test_no_key_allows_local(self, client, monkeypatch):
        # 未配置密钥时本机来源放行（TestClient 的 host 即 "testclient"）
        monkeypatch.delenv("WEB_API_KEY", raising=False)
        assert client.get("/api/config/skills").status_code == 200


class TestApprovalConcurrency:
    """审批钩子并发隔离：contextvars 让并发请求各收各的审批事件。"""

    def test_concurrent_requests_isolated(self):
        import asyncio

        async def scenario():
            hook = server._web_approval_hook()
            q1: asyncio.Queue = asyncio.Queue()
            q2: asyncio.Queue = asyncio.Queue()

            async def request_approval(queue, tool):
                ctx = server._current_approval_queue.set(queue)
                try:
                    return await hook(tool, "{}")
                finally:
                    server._current_approval_queue.reset(ctx)

            t1 = asyncio.create_task(request_approval(q1, "run_command"))
            t2 = asyncio.create_task(request_approval(q2, "write_file"))
            ev1 = await asyncio.wait_for(q1.get(), 2)
            ev2 = await asyncio.wait_for(q2.get(), 2)

            # 各队列收到各自的请求，审批 id 互不相同
            assert (ev1["type"], ev1["tool"]) == ("approval_request", "run_command")
            assert (ev2["type"], ev2["tool"]) == ("approval_request", "write_file")
            assert ev1["approval_id"] != ev2["approval_id"]

            # 用户决策：q1 批准、q2 拒绝
            server._pending_approvals[ev1["approval_id"]].set_result(True)
            server._pending_approvals[ev2["approval_id"]].set_result(False)
            return await asyncio.gather(t1, t2)

        assert asyncio.run(scenario()) == [True, False]

    def test_outside_request_context_denied(self):
        """上下文之外调用（contextvar 未注入）→ 保守拒绝而非误批准。"""
        import asyncio

        async def scenario():
            hook = server._web_approval_hook()
            return await hook("run_command", "{}")

        assert asyncio.run(scenario()) is False
