"""治理层接线测试：G1 scope 接入 run_bash + G5 per-job 审计落盘。

覆盖：
- 目标提取（URL host / 裸 IP / 文件名不误提取）与回环放行判定
- _scope_check：白名单命中放行 / 未命中拒绝（fail-closed）/ 回环放行 /
  无目标放行 / 开关关闭 / 校验器自身异常 fail-open
- run_bash 端到端：范围外目标被拒（不触网）+ audit 落 scope.denied
- build_scope_validator：entries + SECPT_SCOPE_ALLOW 组合、关闭开关、双空不启用
- 审计：scope.denied + job 生命周期事件 read_all 读回
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from secpt.governance.audit import AuditLogger
from secpt.governance.scope import ScopeValidator, build_scope_validator
from secpt.runtime.engine import (
    RunConfig,
    _scope_check,
    extract_scope_targets,
    is_loopback_target,
    make_tool_fns,
)


def _cfg(tmp_path: Path, allowed: list[str]) -> RunConfig:
    cfg = RunConfig(
        job_id="gov-test",
        objective="测试任务",
        system_extra="",
        workspace=tmp_path,
        bash_whitelist=frozenset({"curl", "python3", "pwd", "ls"}),
    )
    cfg.scope = ScopeValidator(allowed)
    cfg.audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    (tmp_path / "ws").mkdir(exist_ok=True)  # run_bash 的 cwd=work_dir 需真实存在
    return cfg


class TestExtractTargets:
    def test_url_host(self):
        assert "10.0.176.0" in extract_scope_targets(
            'curl -s http://10.0.176.0:80/api "BENCHMARK_TOKEN: x"'
        )

    def test_bare_ip_and_port(self):
        assert "192.168.1.198" in extract_scope_targets("curl http://192.168.1.198/x")

    def test_filename_not_target(self):
        # 脚本文件名/输出文件不是网络目标
        assert extract_scope_targets("python3 exploit.py > outputs/bash-1.log") == []
        assert extract_scope_targets("grep flag report.md") == []

    def test_local_command_no_target(self):
        assert extract_scope_targets("pwd && ls -la") == []

    def test_bare_ipv6(self):
        """G1 补丁：裸/括号 IPv6 必须能提取（提取不到即不校验 = 绕过面）。"""
        assert "2001:db8::1" in extract_scope_targets("nmap -6 -p 80 2001:db8::1")
        assert "::1" in extract_scope_targets("curl -g 'http://[::1]/x'")
        assert "fe80::1" in extract_scope_targets("nmap -6 fe80::1%eth0")

    def test_ipv6_false_positives_rejected(self):
        # 时间串 / MAC 地址不是 IP，宽松候选必须被 ipaddress 校验剔除
        assert extract_scope_targets("echo 12:34:56 done") == []
        assert extract_scope_targets("ping aa:bb:cc:dd:ee:ff") == []


class TestLoopback:
    def test_ipv4_loopback_range(self):
        # 127.0.0.0/8 全段放行（SSRF 探测 127.0.0.1:3000 是合法行为）
        assert is_loopback_target("127.0.0.1")
        assert is_loopback_target("127.5.5.5")

    def test_ipv6_and_localhost(self):
        assert is_loopback_target("::1")
        assert is_loopback_target("localhost")
        assert is_loopback_target("api.localhost")

    def test_external_not_loopback(self):
        assert not is_loopback_target("192.168.1.198")
        assert not is_loopback_target("example.com")


class TestScopeCheck:
    def test_hit_allow(self, tmp_path):
        cfg = _cfg(tmp_path, ["10.0.0.0/8"])
        assert _scope_check(cfg, "curl http://10.1.2.3/x") is None

    def test_miss_deny_and_audit(self, tmp_path):
        cfg = _cfg(tmp_path, ["10.0.0.0/8"])
        err = _scope_check(cfg, "curl http://192.168.1.198/x")
        assert err and err.startswith("Error:")
        events = [r["event"] for r in AuditLogger(str(tmp_path / "audit.jsonl")).read_all()]
        assert "scope.denied" in events

    def test_loopback_bypasses_scope(self, tmp_path):
        # 回环目标不参与校验：白名单只有外网段，请求 127.0.0.1 仍放行
        cfg = _cfg(tmp_path, ["10.0.0.0/8"])
        assert _scope_check(cfg, "curl http://127.0.0.1:3000/probe") is None

    def test_no_target_command_allowed(self, tmp_path):
        cfg = _cfg(tmp_path, ["10.0.0.0/8"])
        assert _scope_check(cfg, "pwd") is None

    def test_kill_switch(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_SCOPE", "0")
        cfg = _cfg(tmp_path, ["10.0.0.0/8"])
        assert _scope_check(cfg, "curl http://192.168.1.198/x") is None

    def test_validator_crash_failopen_and_audit(self, tmp_path):
        # 校验器自身异常（如白名单配置坏）→ fail-open 放行 + audit(scope.error)
        cfg = _cfg(tmp_path, ["10.0.0.0/8"])

        class _Broken:
            def validate(self, target):
                raise RuntimeError("boom")

        cfg.scope = _Broken()
        assert _scope_check(cfg, "curl http://10.1.2.3/x") is None
        events = [r["event"] for r in AuditLogger(str(tmp_path / "audit.jsonl")).read_all()]
        assert "scope.error" in events


class TestRunBashWiring:
    def test_run_bash_deny_out_of_scope(self, tmp_path):
        """端到端：范围外目标在执行前被拒（不触网），返回可行动的错误。"""
        cfg = _cfg(tmp_path, ["10.0.0.0/8"])
        fns = make_tool_fns(cfg)
        out = asyncio.run(fns["bash"]({"command": "curl -s http://192.168.1.198/"}))
        assert out.startswith("Error:")
        assert "SECPT_SCOPE_ALLOW" in out  # 告知模型扩展方式
        records = AuditLogger(str(tmp_path / "audit.jsonl")).read_all()
        assert records[-1]["event"] == "scope.denied"

    def test_run_bash_local_command_ok(self, tmp_path):
        cfg = _cfg(tmp_path, ["10.0.0.0/8"])
        fns = make_tool_fns(cfg)
        out = asyncio.run(fns["bash"]({"command": "pwd"}))
        assert not out.startswith("Error:")


class TestBuildValidator:
    def test_entries_plus_env(self, monkeypatch):
        monkeypatch.delenv("SECPT_SCOPE", raising=False)
        monkeypatch.setenv("SECPT_SCOPE_ALLOW", "172.16.0.0/12, example.com")
        v = build_scope_validator(["10.0.0.5"])
        assert v is not None
        assert v.is_allowed("10.0.0.5")  # entries
        assert v.is_allowed("172.16.1.1")  # env CIDR
        assert v.is_allowed("sub.example.com")  # env 域名（含子域名）
        assert not v.is_allowed("8.8.8.8")

    def test_kill_switch_returns_none(self, monkeypatch):
        monkeypatch.setenv("SECPT_SCOPE", "0")
        assert build_scope_validator(["10.0.0.5"]) is None

    def test_empty_sources_returns_none(self, monkeypatch):
        monkeypatch.delenv("SECPT_SCOPE", raising=False)
        monkeypatch.delenv("SECPT_SCOPE_ALLOW", raising=False)
        assert build_scope_validator([]) is None  # 双空不启用（避免全拒瘫痪）

    def test_wildcard_allow_all(self, monkeypatch):
        monkeypatch.delenv("SECPT_SCOPE", raising=False)
        monkeypatch.setenv("SECPT_SCOPE_ALLOW", "0.0.0.0/0")
        v = build_scope_validator([])
        assert v is not None and v.is_allowed("8.8.8.8")


class TestAuditLifecycle:
    def test_denied_and_lifecycle_read_back(self, tmp_path):
        """验收：一条 scope denied + 一条 job 生命周期能被 read_all 读回。"""
        path = str(tmp_path / "audit" / "gov-test.jsonl")
        al = AuditLogger(path)
        # scope.denied：经 engine _scope_check 真实路径写入
        cfg = _cfg(tmp_path, ["10.0.0.0/8"])
        cfg.audit = al
        _scope_check(cfg, "curl http://192.168.1.198/x")
        # job 生命周期：与 run_engine/worker 落点同构
        al.log("job.started", profile="ctf", objective="obj")
        al.log("job.finished", status="succeeded", steps=42, wall_s=12.5)
        events = [r["event"] for r in al.read_all()]
        assert events == ["scope.denied", "job.started", "job.finished"]
        rec = AuditLogger(path).read_all()[-1]
        assert rec["status"] == "succeeded" and rec["steps"] == 42


class TestPluginGovernance:
    """x-secpt-governance.network 插件工具统一治理前置（A 任务接线）。"""

    @staticmethod
    def _engine(tmp_path: Path, allowed: list[str], fn):
        from secpt.runtime.engine import Engine

        cfg = _cfg(tmp_path, allowed)
        schema = {
            "type": "function",
            "function": {
                "name": "browser_navigate",
                "description": "测试用网络插件",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                    "x-secpt-governance": {"network": True},
                },
            },
        }
        return Engine(cfg, llm=object(), extra_tools={"browser_navigate": (schema, fn)})

    def test_scope_denies_out_of_scope(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_INTERCEPT", "0")
        called = []

        async def _fn(args):
            called.append(args)
            return "ok"

        eng = self._engine(tmp_path, ["10.0.0.0/8"], _fn)
        out = asyncio.run(eng.tools["browser_navigate"]({"url": "http://evil.example/x"}))
        assert out.startswith("Error:") and "授权范围" in out
        assert called == []  # 治理拒绝 → 原工具未执行

    def test_scope_allows_in_scope(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECPT_INTERCEPT", "0")
        called = []

        async def _fn(args):
            called.append(args)
            return "ok"

        eng = self._engine(tmp_path, ["10.0.0.0/8"], _fn)
        out = asyncio.run(eng.tools["browser_navigate"]({"url": "http://10.1.2.3/x"}))
        assert out == "ok" and called

    def test_intercept_deny_blocks_plugin(self, tmp_path, monkeypatch):
        from secpt.governance.intercept import BLOCK_TAG

        class _Deny:
            def __init__(self, *a, **k): ...

            def eval(self, *, tool, command, job_id="", wait=True):
                return False, f"{BLOCK_TAG} 规则[测试]:禁止"

        monkeypatch.setattr("secpt.governance.intercept.Interceptor", _Deny)
        called = []

        async def _fn(args):
            called.append(args)
            return "ok"

        eng = self._engine(tmp_path, ["10.0.0.0/8"], _fn)
        out = asyncio.run(eng.tools["browser_navigate"]({"url": "http://10.1.2.3/x"}))
        assert "测试" in out and called == []  # 审批拒绝 → 原工具未执行

    def test_non_governed_plugin_untouched(self, tmp_path, monkeypatch):
        """未声明治理的插件工具不套前置（普通插件零开销）。"""
        from secpt.runtime.engine import Engine

        monkeypatch.setenv("SECPT_INTERCEPT", "0")
        schema = {
            "type": "function",
            "function": {
                "name": "calc",
                "description": "本地计算",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        called = []

        async def _fn(args):
            called.append(args)
            return "42"

        cfg = _cfg(tmp_path, [])
        eng = Engine(cfg, llm=object(), extra_tools={"calc": (schema, _fn)})
        assert asyncio.run(eng.tools["calc"]({})) == "42"
        assert called

    def test_llm_schema_strips_private_keys(self, tmp_path):
        """x-secpt-governance 是 engine 私有协议，不得随 schema 发往 LLM。"""

        async def _fn(args):
            return "ok"

        eng = self._engine(tmp_path, ["10.0.0.0/8"], _fn)
        params = eng._extra_schemas[0]["function"]["parameters"]
        assert "x-secpt-governance" not in params
        assert params["properties"]
