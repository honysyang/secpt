"""M5-28 只读情报工具：http_get 证据落盘 + read_file/list_dir 的根边界。

全部离线：http_get 注入 fake opener（零网络），文件类工具用 tmp_path。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from secpt.tools.readonly import HttpGetTool, ListDirTool, ReadFileTool

_BODY = b"<tr><td>sub.example.com</td></tr>"


def _opener(_url: str, _timeout_s: float) -> tuple[int, Mapping[str, str], bytes]:
    """固定响应：验证"响应必须落成 raw 文件"的诚实锚点。"""
    return 200, {"content-type": "text/html"}, _BODY


def test_http_get_writes_raw_evidence(tmp_path: Path) -> None:
    tool = HttpGetTool(raw_dir=tmp_path, opener=_opener)
    out = tool.run({"url": "https://crt.sh/?q=example.com"})

    assert out.exit_code == 0
    assert out.raw_ref and Path(out.raw_ref).is_file()
    text = Path(out.raw_ref).read_text(encoding="utf-8")
    assert "$ http_get https://crt.sh/?q=example.com" in text  # 请求本身也有账
    assert "--- status ---\n200" in text
    assert "sub.example.com" in text
    assert "HTTP 200" in out.summary


def test_http_get_rejects_non_http_scheme(tmp_path: Path) -> None:
    """file:// 之类一律拒绝：网络出口只能是 http/https。"""
    out = HttpGetTool(raw_dir=tmp_path, opener=_opener).run({"url": "file:///etc/passwd"})
    assert out.exit_code is None
    assert "http/https" in out.summary
    assert out.raw_ref == ""


def test_http_get_network_failure_is_receipt(tmp_path: Path) -> None:
    """网络失败落 E_TOOL_FAIL 回执，绝不抛错打断主循环。"""

    def boom(_url: str, _timeout_s: float) -> tuple[int, Mapping[str, str], bytes]:
        raise OSError("DNS 解析失败")

    out = HttpGetTool(raw_dir=tmp_path, opener=boom).run({"url": "https://x.invalid/"})
    assert out.exit_code is None
    assert "E_TOOL_FAIL" in out.summary and "DNS" in out.summary
    assert out.raw_ref == ""


def test_read_file_bounded_to_roots(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "scan.txt").write_text("22/tcp open ssh\n", encoding="utf-8")
    tool = ReadFileTool(roots=(raw,))

    ok = tool.run({"path": "scan.txt"})
    assert ok.exit_code == 0 and "22/tcp open ssh" in ok.summary
    assert ok.raw_ref and Path(ok.raw_ref).is_file()  # 读到的文件就是证据锚

    escape = tool.run({"path": "../etc/passwd"})
    assert escape.exit_code is None and "只读根" in escape.summary

    # M5-33：绝对路径在根外仍拒（安全边界不变），在根内放行（成功摘要回显的
    # 就是绝对路径，模型照抄不该被拒——实测一轮 4 次失败全由此来）。
    outside = tool.run({"path": "/etc/passwd"})
    assert outside.exit_code is None and "只读根" in outside.summary
    inside = tool.run({"path": str(raw / "scan.txt")})
    assert inside.exit_code == 0 and "22/tcp open ssh" in inside.summary


def test_read_file_accepts_cwd_relative_raw_prefix(tmp_path: Path, monkeypatch) -> None:
    """M5-30：模型照抄 http_get 的 raw_ref（raw/xxx.txt）也必须读得到。"""
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "http_a.txt").write_text("HTTP 200 body\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    tool = ReadFileTool(roots=("raw",))

    out = tool.run({"path": "raw/http_a.txt"})
    assert out.exit_code == 0 and "HTTP 200 body" in out.summary


def test_list_dir_accepts_cwd_relative_raw_prefix(tmp_path: Path, monkeypatch) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "a.txt").write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    out = ListDirTool(roots=("raw",)).run({"path": "raw"})
    assert out.exit_code == 0 and "a.txt" in out.summary


def test_list_dir_bounded_to_roots(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "recon.md").write_text("x", encoding="utf-8")
    tool = ListDirTool(roots=(tmp_path / "raw", evidence))

    ok = tool.run({"path": "."})
    assert ok.exit_code == 0 and "recon.md" in ok.summary

    escape = tool.run({"path": "../.."})
    assert escape.exit_code is None and "只读根" in escape.summary


def test_passive_tools_are_read_only() -> None:
    """R1 重放资格：三件套都是只读声明（写操作永不 rerun 的红线不受影响）。"""
    assert HttpGetTool.manifest.read_only
    assert ReadFileTool.manifest.read_only
    assert ListDirTool.manifest.read_only


def test_http_get_evidence_can_be_verified_by_r1(tmp_path: Path) -> None:
    """端到端接缝：http_get 的 raw 锚 → R1 只读重放 → verified(rerun)。

    这条链是被动侦察能否"产出可核证据"的关键：没有它，worker 拿到的响应只能
    停在 claimed，出场永远卡在悬置。opener 固定响应 = 重放内容稳定。
    """
    from secpt.judge.r123 import r1
    from secpt.store import ledger, migrate
    from secpt.tools.registry import ToolRegistry

    conn = migrate.connect(":memory:")
    task = ledger.start_task(conn, "被动侦察演示", goal="crt.sh 子域")
    run = ledger.start_run(conn, task.id)
    step = ledger.record_step(conn, task.id, run.id, 1, "{}")
    url = "https://crt.sh/?q=example.com"
    registry = ToolRegistry([HttpGetTool(raw_dir=tmp_path, opener=_opener)])

    out = registry.dispatch("http_get", {"url": url})
    assert out.exit_code == 0
    ledger.append_ticket(
        conn,
        task.id,
        run.id,
        step.id,
        "http_get",
        {"url": url},
        out.exit_code,
        out.summary,
        out.raw_ref,
    )
    finding = ledger.append_finding(conn, task.id, "crt.sh 暴露子域", "证书日志可见", out.raw_ref)

    verdict = r1(conn, finding, registry.readonly_replay, None)
    assert verdict is not None
    assert verdict.verify_state == "verified" and verdict.method == "rerun"
    conn.close()


# ---- M5-31 感知带宽：offset 续读 / 关键响应头进摘要 / 重复抓取提示 ----------------


def test_read_file_offset_reads_tail(tmp_path: Path) -> None:
    """offset 语义：1 起始行号；续读摘要明说"本读区间/剩余行数/怎么接着读"。"""
    target = tmp_path / "big.txt"
    target.write_text("\n".join(f"line-{n}" for n in range(1, 11)) + "\n", encoding="utf-8")
    tool = ReadFileTool(roots=(tmp_path,))
    head = tool.run({"path": "big.txt", "max_lines": "4"})
    assert "共 10 行，本读 1..4" in head.summary
    assert "剩 6 行未读：offset=5 续读" in head.summary
    tail = tool.run({"path": "big.txt", "max_lines": "4", "offset": "5"})
    assert "本读 5..8" in tail.summary
    assert "line-5" in tail.summary  # 摘要带的是本读区间的开头，不再永远是文件头


def test_http_get_summary_carries_key_headers(tmp_path: Path) -> None:
    """Server/ETag/长度等指纹头直接进摘要：版本判别与 SPA 回退对比不必再翻 raw。"""

    def _opener(url: str, timeout_s: float) -> tuple[int, dict[str, str], bytes]:
        return (
            200,
            {
                "Server": "nginx/1.18.0",
                "ETag": '"abc123"',
                "Content-Type": "text/html",
            },
            b"<html>ok</html>",
        )

    tool = HttpGetTool(raw_dir=tmp_path, opener=_opener)
    out = tool.run({"url": "https://x.example.com/"})
    assert "Server: nginx/1.18.0" in out.summary
    assert 'ETag: "abc123"' in out.summary


def test_http_get_repeat_hint_names_first_capture(tmp_path: Path) -> None:
    """同 URL 重复抓取：摘要点名"第 N 次 + 首见产物路径"（不做缓存，R1 重放必须真复跑）。"""
    tool = HttpGetTool(raw_dir=tmp_path, opener=_opener)
    first = tool.run({"url": "https://x.example.com/"})
    assert "第 1 次" not in first.summary  # 首抓不提示
    second = tool.run({"url": "https://x.example.com/"})
    third = tool.run({"url": "https://x.example.com/"})
    assert "本 run 第 2 次抓取同 URL" in second.summary
    assert f"首见 {first.raw_ref}" in second.summary
    assert "本 run 第 3 次抓取同 URL" in third.summary


# ---- M5-32 内容进摘要 / 超时上限 / list_dir 新→旧 ---------------------------------


def test_read_file_small_file_fully_visible_in_summary(tmp_path: Path) -> None:
    """16 行小文件：正文整段折叠进摘要——模型一次读完，不必同参数重读 7 次。"""
    target = tmp_path / "small.txt"
    lines = [f'{{"item": "s{i}.example.com"}}' for i in range(1, 17)]
    target.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    out = ReadFileTool(roots=(tmp_path,)).run({"path": "small.txt", "max_lines": "120"})
    assert "本读 1..16" in out.summary
    header = out.summary.split("：", 1)[0]
    assert "剩" not in header  # 读全了，没有剩余行提示
    assert '"item": "s16.example.com"' in out.summary  # 最后一行也在摘要里
    assert "已截断" not in out.summary


def test_read_file_big_slice_truncates_with_hint(tmp_path: Path) -> None:
    """超上限的段落：内容截断 + 明说"用 offset 分段读"。"""
    target = tmp_path / "big.txt"
    target.write_text(chr(10).join("y" * 60 for _ in range(60)), encoding="utf-8")
    out = ReadFileTool(roots=(tmp_path,)).run({"path": "big.txt", "max_lines": "60"})
    assert "本读 1..60" in out.summary
    assert "已截断" in out.summary and "offset" in out.summary


def test_http_get_timeout_clamped_to_30s(tmp_path: Path) -> None:
    """M5-32：模型传 40s 超时被夹回默认 20s——被动源查询没有合法的 40s+ 场景。"""
    seen: dict[str, float] = {}

    def _recorder(url: str, timeout_s: float) -> tuple[int, dict[str, str], bytes]:
        seen["timeout"] = timeout_s
        return 200, {}, b"ok"

    HttpGetTool(raw_dir=tmp_path, opener=_recorder).run(
        {"url": "https://x.example.com/", "timeout_s": "40"}
    )
    assert seen["timeout"] == 20.0


def test_list_dir_newest_first(tmp_path: Path) -> None:
    """跨 run 的 raw 目录按修改时间新→旧：本 run 的产物排前面，不被旧文件挤出。"""
    import os

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    old = evidence / "old_run.txt"
    new = evidence / "new_run.txt"
    old.write_text("old", encoding="utf-8")
    new.write_text("new", encoding="utf-8")
    os.utime(old, (1_000_000_000, 1_000_000_000))
    os.utime(new, (2_000_000_000, 2_000_000_000))
    out = ListDirTool(roots=(tmp_path / "raw", evidence)).run({"path": "."})
    assert out.exit_code == 0
    assert out.summary.index("new_run.txt") < out.summary.index("old_run.txt")
