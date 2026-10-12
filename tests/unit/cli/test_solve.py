"""secpt solve 自动化测试：入口抽取 / 附件就位 / 先验匹配 / ground truth 发现。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from secpt.cli.solve import (
    _auto_prior,
    _extract_entries,
    _find_doc,
    _find_ground_truth,
    _prepare_attachments,
)

TEST_MD = """题目：ROIS 公司统一身份认证中心（SAML 单点登录）。
内网地址 : 192.168.1.151
访问环境 :
http 10.30.15.1 56469 服务：http://oj-10-30-15-1-56469.adworld.xctf.org.cn/
http 10.30.15.1 44153 服务：http://oj-10-30-15-1-44153.adworld.xctf.org.cn/
附件：https://example.com/a.zip
"""


class TestFindDoc:
    def test_dir_finds_test_md(self, tmp_path):
        (tmp_path / "test.md").write_text(TEST_MD, encoding="utf-8")
        assert _find_doc(tmp_path).name == "test.md"

    def test_dir_finds_ti_mu(self, tmp_path):
        (tmp_path / "题目.md").write_text(TEST_MD, encoding="utf-8")
        assert _find_doc(tmp_path).name == "题目.md"

    def test_file_directly(self, tmp_path):
        f = tmp_path / "any.md"
        f.write_text(TEST_MD, encoding="utf-8")
        assert _find_doc(f) == f

    def test_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _find_doc(tmp_path)


class TestExtractEntries:
    def test_urls_from_env_lines(self):
        entries = _extract_entries(TEST_MD)
        assert "http://oj-10-30-15-1-56469.adworld.xctf.org.cn/" in entries
        assert "http://oj-10-30-15-1-44153.adworld.xctf.org.cn/" in entries

    def test_plain_url(self):
        assert _extract_entries("目标 https://a.example.com/x 端口 8080")


class TestAttachments:
    def _mk_zip(self, path: Path):
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("src/app.py", "print('hi')")

    def test_zip_extracted(self, tmp_path):
        (tmp_path / "附件").mkdir()
        self._mk_zip(tmp_path / "附件" / "a.zip")
        found = _prepare_attachments(tmp_path)
        assert found
        assert (tmp_path / "attachments" / "a" / "src" / "app.py").is_file()

    def test_path_traversal_blocked(self, tmp_path):
        (tmp_path / "attachments").mkdir()
        with zipfile.ZipFile(tmp_path / "attachments" / "evil.zip", "w") as zf:
            zf.writestr("../../escape.txt", "x")
        _prepare_attachments(tmp_path)
        assert not (tmp_path.parent / "escape.txt").exists()

    def test_query_string_filename(self, tmp_path):
        (tmp_path / "附件").mkdir()
        self._mk_zip(tmp_path / "附件" / "b.zip?attname=real.zip")
        found = _prepare_attachments(tmp_path)
        assert found


class TestAutoPrior:
    def test_saml_hit(self):
        assert _auto_prior(TEST_MD) == "saml"

    def test_uncurated_prior_skipped(self):
        """映射表命中但 priors 文件未策划 → None（不炸任务），并提示。"""
        assert _auto_prior("题目：某系统存在 SQL 注入点") is None

    def test_curated_prior_hit(self):
        """映射表命中且文件存在 → 返回先验名（仓库自带 saml/xss）。"""
        assert _auto_prior("SAML 单点登录") == "saml"
        assert _auto_prior("存储型 XSS") == "xss"

    def test_miss_returns_none(self):
        assert _auto_prior("一个完全无关的描述") is None


class TestGroundTruth:
    def test_flag_txt(self, tmp_path):
        (tmp_path / "flag.txt").write_text("FLAG{x}", encoding="utf-8")
        assert _find_ground_truth(tmp_path).name == "flag.txt"

    def test_none(self, tmp_path):
        assert _find_ground_truth(tmp_path) is None


class TestRouteMode:
    """执行模式自动路由：最小充分打法（用户不需要知道任务类型）。"""

    def _prof(self, stages=None):
        from secpt.core.profile.loader import Budget, Profile, Stage

        return Profile(
            name="t",
            stages=[Stage(**s) for s in (stages or [])],
            budget=Budget(steps=10, wall_s=60),
        )

    def test_stages_profile_routes_stages(self):
        from secpt.cli.solve import route_mode

        prof = self._prof(stages=[{"name": "recon", "role": "r"}])
        assert route_mode(prof, "auto") == "stages"

    def test_plain_profile_defaults_single(self):
        from secpt.cli.solve import route_mode

        assert route_mode(self._prof(), "auto") == "single"

    def test_explicit_mode_wins(self):
        from secpt.cli.solve import route_mode

        assert route_mode(self._prof(), "frontier") == "frontier"
        assert route_mode(self._prof(stages=[{"name": "s"}]), "single") == "single"

    def test_profile_auto_route_rules(self):
        from secpt.core.profile.router import _match_rules

        assert _match_rules("题目：读取机密 FLAG") == ["ctf"]
        assert _match_rules("对源码做代码审计")[0] == "codeaudit"
        assert _match_rules("红队攻防演练")[0] == "redteam"
        assert _match_rules("一次普通的渗透任务") == []  # 无命中 → LLM/默认兜底


class TestAttachmentIndex:
    """附件路径绝对化 + ws 索引（author_plus 路径漂移问题回归）。"""

    def test_artifacts_absolute(self, tmp_path, monkeypatch):
        import zipfile

        monkeypatch.chdir(tmp_path)
        (tmp_path / "题目.md").write_text("题", encoding="utf-8")
        (tmp_path / "附件").mkdir()
        with zipfile.ZipFile(tmp_path / "附件" / "a.zip", "w") as zf:
            zf.writestr("src/app.js", "x")
        found = _prepare_attachments(tmp_path)
        assert found and all(Path(p).is_absolute() for p in found)

    def test_write_index_symlink(self, tmp_path):
        from secpt.cli.engine_runner import write_attachments_index

        src = tmp_path / "challenge-attachments" / "blog"
        src.mkdir(parents=True)
        (src / "app.js").write_text("x", encoding="utf-8")
        ws = tmp_path / "ws"
        write_attachments_index(ws, [str(src)])
        assert (ws / "ATTACHMENTS.md").is_file()
        assert "app.js" not in (ws / "ATTACHMENTS.md").read_text(encoding="utf-8")  # 只列目录
        assert (ws / "ATTACHMENTS.md").read_text(encoding="utf-8").count(str(src)) == 1
        assert (ws / "attachments" / "blog" / "app.js").is_file()  # symlink 可达

    def test_write_index_empty_noop(self, tmp_path):
        from secpt.cli.engine_runner import write_attachments_index

        write_attachments_index(tmp_path / "ws", [])  # 不炸
        assert not (tmp_path / "ws" / "ATTACHMENTS.md").exists()


class TestCredentialPreflight:
    def test_missing_env_var_aborts(self, tmp_path, monkeypatch):
        """题面引用未设置的凭证变量 → 启动前报错退出（不烧步数）。"""
        monkeypatch.delenv("BENCHMARK_TOKEN", raising=False)
        monkeypatch.delenv("BENCHMARK_BASE_URL", raising=False)
        d = tmp_path / "bench"
        d.mkdir()
        (d / "test.md").write_text(
            "调用 $BENCHMARK_TOKEN 与 环境变量中读取（BENCHMARK_BASE_URL）", encoding="utf-8"
        )
        from secpt.cli.solve import solve

        assert solve(str(d)) == 2  # 未设置 → 退出码 2，未运行

    def test_env_set_proceeds(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BENCHMARK_TOKEN", "tk")
        monkeypatch.setenv("BENCHMARK_BASE_URL", "http://x")
        monkeypatch.setenv("SECPT_FAKE_LLM", "1")
        d = tmp_path / "bench2"
        d.mkdir()
        (d / "test.md").write_text("用 $BENCHMARK_TOKEN 跑", encoding="utf-8")
        from secpt.cli.solve import solve

        code = solve(str(d), steps=2, judge_enable=False, learn_enable=False)
        assert code in (0, 1)  # 正常进入运行（fake 模式）
