"""P2 统一编排策略：route_policy 路由 + prove_goal 量化验收。"""

from __future__ import annotations

from pathlib import Path

from secpt.core.orchestrator.policy import prove_goal, route_policy


class TestRoutePolicy:
    def test_benchmark_routes_to_benchmark(self):
        """跑关：benchmark → 直接走并行跑分器。"""
        assert route_policy("benchmark", ["http://a"]) == "benchmark"
        assert route_policy("benchmark", []) == "benchmark"

    def test_multi_entry_routes_to_team(self):
        """渗透：多入口 → frontier 角色编排（recon→exploit×N 并行）。"""
        assert route_policy("ctf", ["http://a", "http://b"]) == "team"
        assert route_policy("pentest", ["http://a", "http://b", "http://c"]) == "team"

    def test_single_entry_routes_to_direct(self):
        """单入口/无入口 → 直连单链（编排层休眠）。"""
        assert route_policy("ctf", ["http://a"]) == "direct"
        assert route_policy("ctf", []) == "direct"
        assert route_policy("pentest", ["http://a"]) == "direct"


class TestProveGoal:
    def test_file_criterion_passes_when_present(self, tmp_path):
        """文件落盘判据：文件存在 → 通过。"""
        (tmp_path / "assets.json").write_text("[]")
        ok, _ = prove_goal("产出结构化资产清单 assets.json 落盘", "侦察完成", job_ws=tmp_path)
        assert ok

    def test_file_criterion_fails_when_missing(self, tmp_path):
        """文件落盘判据：文件缺失 → 不通过（防止侦察空转被标 done）。"""
        ok, why = prove_goal("产出 assets.json 落盘", "侦察完成", job_ws=tmp_path)
        assert not ok and "assets.json" in why

    def test_flag_criterion_requires_real_flag(self):
        """flag 判据（无"或"）：answer 无真实 flag 形态 → 不通过（防假成功）。"""
        ok, why = prove_goal("拿到 flag", "unique_code=g-05 已 close")
        assert not ok and "真实 flag" in why

    def test_flag_criterion_passes_with_real_flag(self):
        assert prove_goal("拿到 flag", "FLAG{pwned}")[0]

    def test_or_criteria_fail_open(self):
        """含"或"的判据是多路成功语义：无 flag 也放行（pentest 确认漏洞合法）。"""
        ok, _ = prove_goal("拿到 flag 或确认漏洞", "确认存在 SQLi 漏洞")
        assert ok

    def test_no_criterion_fail_open(self):
        """无显式 flag/文件判据：自然语言判据 fail-open 放行。"""
        assert prove_goal("有指纹信息", "nginx/1.18 指纹已拿到")[0]
        assert prove_goal("", "随便答")[0]

    def test_exception_fail_open(self, monkeypatch):
        """验收器异常 fail-open 放行（不阻断编排）。"""
        import secpt.core.orchestrator.policy as pol

        class _Boom:
            @staticmethod
            def findall(_s):
                raise OSError("boom")

        monkeypatch.setattr(pol, "_FILE_MARKER_RE", _Boom)
        ok, why = prove_goal("产出 assets.json 落盘", "x", job_ws=Path("/tmp/nowhere"))
        assert ok and "放行" in why
