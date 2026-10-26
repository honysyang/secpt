"""附件/文件任务与 0day 研究能力验收。

覆盖：
- collect/stage：目录递归、上限、二进制检测、确定性命名（重名跳过）；
- 纯文件任务：无目标 + 附件 → file.scan 验收可达成 → completed；
- filerules：密钥/危险 sink 规则、占位符不误报、依赖解析 + advisory 匹配；
- fs grep：定点扫描、路径逃逸仍然被拦；
- bootstrap 扫描：findings 走 JEV、cred 线索脱敏、file.scan 线索落账。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pa.assets.snapshot import AssetSnapshot
from pa.core import filerules, nl
from pa.infra.llm import MockLLM
from pa.report.render import render_report
from pa.runtime import run
from pa.store.view import StoreView
from pa.tools import REGISTRY, ToolContext

ROOT = Path(__file__).resolve().parent.parent


# ── collect / stage ──────────────────────────────────────────


def test_collect_files_dir_recursive_and_warnings(tmp_path):
    d = tmp_path / "src"
    (d / "pkg").mkdir(parents=True)
    (d / "app.py").write_text("x = 1\n", encoding="utf-8")
    (d / "pkg" / "util.py").write_text("y = 2\n", encoding="utf-8")
    (d / "big.bin").write_bytes(b"\x00" * 100)
    (d / "link").symlink_to(d / "app.py")
    files, warns = nl.collect_files([d])
    names = [p.name for p, _ in files]
    assert "app.py" in names and "util.py" in names and "big.bin" in names
    assert "link" not in names, "符号链接不收录"
    missing = tmp_path / "nope.txt"
    _, warns2 = nl.collect_files([missing])
    assert any("不存在" in w for w in warns2)


def test_stage_files_deterministic_names_and_binary(tmp_path):
    src = tmp_path / "app.py"
    src.write_text("print(1)\n", encoding="utf-8")
    binf = tmp_path / "blob.bin"
    binf.write_bytes(b"\x00\x01\x02", )
    dest = tmp_path / "ws" / "_attachments"
    refs, warns = nl.stage_files([(src, 8), (binf, 3)], dest)
    assert [r.name for r in refs] == ["_attachments/app.py", "_attachments/blob.bin"]
    assert (dest / "app.py").read_text() == "print(1)\n"
    binary_ref = next(r for r in refs if r.binary)
    assert binary_ref.name.endswith("blob.bin")
    assert all(len(r.sha256) == 16 for r in refs)
    # 重名确定性跳过
    refs2, warns2 = nl.stage_files([(src, 8)], dest)
    assert refs2 == [] and any("重名" in w for w in warns2)


# ── 纯文件任务：无目标 + 附件 → file.scan 验收 ────────────────


def test_build_spec_file_only_mission(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_LLM_MOCK", json.dumps({"do": ['{"items": []}']}))
    src = tmp_path / "app.py"
    src.write_text("x = 1\n", encoding="utf-8")
    spec, warns = nl.build_spec("审计这个附件", llm=MockLLM({"do": ["{}"]}), attachments=[src])
    assert spec.targets == ["app.py"]
    assert spec.acceptance[0].evidence == "file.scan"
    assert spec.acceptance[0].target == "app.py"
    assert "全部 1 个附件" in spec.acceptance[0].item
    assert any("file.scan" in w for w in warns)


def test_no_targets_and_no_files_still_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_LLM_MOCK", json.dumps({"do": ['{"items": []}']}))
    with pytest.raises(Exception):
        nl.build_spec("随便看看", llm=MockLLM({"do": ["{}"]}))


# ── filerules 单元 ───────────────────────────────────────────


def test_scan_text_secrets_and_redaction():
    code = (
        'AWS_ID = "AKIAIOSFODNN7EXAMPLE"\n'
        "token = 'ghp_' + 'A'*36 + ''\n"
        "password = 'hunter2hunter2'\n"
        "PASSWORD = 'changeme-please'  # 占位符不报\n"
        "x = 1\n"
    )
    token = "ghp_" + "A" * 36
    code = code.replace("'ghp_' + 'A'*36 + ''", f'"{token}"')
    hits = filerules.scan_text(code)
    rules = {h.rule for h in hits}
    assert {"aws_access_key", "github_token", "generic_credential"} <= rules
    aws = next(h for h in hits if h.rule == "aws_access_key")
    assert aws.severity == "high" and aws.secret
    assert "AKIAIOSFODNN7EXAMPLE" not in aws.snippet, "密钥值必须脱敏"
    # 占位符：generic_credential 命中 changeme 行被跳过
    gen = [h for h in hits if h.rule == "generic_credential"]
    assert all("changeme" not in h.snippet for h in gen)


def test_scan_text_dangerous_sinks():
    code = "\n".join(
        [
            "eval(user_input)",
            "subprocess.run(cmd, shell=True)",
            "pickle.loads(blob)",
            "yaml.load(data)",
            "requests.get(url, verify=False)",
            "hashlib.md5(pw)",
            "app.run(debug=True)",
        ]
    )
    hits = filerules.scan_text(code)
    rules = {h.rule for h in hits}
    assert {
        "python_eval",
        "subprocess_shell",
        "pickle_deser",
        "yaml_load",
        "tls_verify_false",
        "weak_hash",
        "flask_debug",
    } <= rules
    assert any(h.severity == "high" for h in hits if h.rule == "subprocess_shell")


def test_parse_deps_and_advisory_match():
    req = "requests==2.6.0\nflask>=2.0.0\npyyaml==5.1\n"
    deps = filerules.parse_deps("requirements.txt", req)
    assert ("pypi", "requests", "2.6.0") in deps
    advisories = [
        {
            "ecosystem": "pypi",
            "name": "requests",
            "introduced": "2.3.0",
            "fixed": "2.31.0",
            "id": "CVE-2023-32681",
            "severity": "med",
        },
        {
            "ecosystem": "pypi",
            "name": "flask",
            "fixed": "1.0.0",
            "id": "OLD",
        },
    ]
    hits = filerules.match_advisories(deps, advisories)
    ids = {h["id"] for h in hits}
    assert ids == {"CVE-2023-32681"}, "flask 2.x 已过 fixed 不应命中"


def test_parse_deps_package_json_and_go_mod():
    pkg = json.dumps(
        {"dependencies": {"lodash": "^4.17.19"}, "devDependencies": {"x": "~1.2.3"}}
    )
    deps = filerules.parse_deps("package.json", pkg)
    assert ("npm", "lodash", "4.17.19") in deps
    gomod = "require (\n\tgithub.com/some/pkg v1.2.3\n)\n"
    deps2 = filerules.parse_deps("go.mod", gomod)
    assert ("go", "github.com/some/pkg", "1.2.3") in deps2


def test_load_advisories_missing_is_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("SECPT_ADVISORIES", raising=False)
    assert filerules.load_advisories() == []


# ── fs grep ──────────────────────────────────────────────────


def test_fs_grep_matches_and_binary_skip(tmp_mission):
    (tmp_mission / "src").mkdir()
    (tmp_mission / "src" / "a.py").write_text(
        "def f():\n    return eval(x)\n", encoding="utf-8"
    )
    (tmp_mission / "src" / "b.py").write_text("safe = 1\n", encoding="utf-8")
    (tmp_mission / "src" / "blob.bin").write_bytes(b"\x00eval(x)\x00")
    ctx = ToolContext(workspace=tmp_mission, timeout=5)
    r = REGISTRY["fs"].fn(
        {"op": "grep", "path": "src", "pattern": "eval\\(", "glob": "*.py"},
        ctx,
    )
    assert r.exit_code == 0
    out = r.stdout.decode()
    assert "src/a.py:2:" in out
    assert "blob.bin" not in out and "b.py:1" not in out
    assert r.meta["matches"] == 1
    # 坏正则 blocked
    r2 = REGISTRY["fs"].fn(
        {"op": "grep", "path": "src", "pattern": "(("}, ctx
    )
    assert r2.exit_code == 126 and b"bad_regex" in r2.stderr


# ── 端到端：附件 mission → 扫描 → file.scan 验收 → completed ──


def _code_planner() -> str:
    return json.dumps(
        {
            "tasks": [
                {
                    "type": "code",
                    "goal": "复核附件扫描结论并审计源码",
                    "success_criteria": [{"evidence": "file.scan"}],
                    "scope_slice": {"files": "_attachments"},
                    "allowed_tools": ["fs"],
                    "role": "analyst",
                    "budget": {"steps": 4},
                }
            ],
            "current_strategy": {"approach": "source-review"},
            "rationale": "引擎已扫描，先复核再深挖",
        },
        ensure_ascii=False,
    )


def test_attachment_mission_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("SECPT_LLM_MOCK", json.dumps({"do": ['{"items": []}']}))
    src = tmp_path / "app.py"
    src.write_text(
        'AWS_ID = "AKIAIOSFODNN7EXAMPLE"\n'
        "data = pickle.loads(payload)\n",
        encoding="utf-8",
    )
    req = tmp_path / "requirements.txt"
    req.write_text("requests==2.6.0\n", encoding="utf-8")
    adv = tmp_path / "adv.jsonl"
    adv.write_text(
        json.dumps(
            {
                "ecosystem": "pypi",
                "name": "requests",
                "introduced": "2.3.0",
                "fixed": "2.31.0",
                "id": "CVE-2023-32681",
                "severity": "med",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SECPT_ADVISORIES", str(adv))

    files, _ = nl.collect_files([src, req])
    spec, _ = nl.build_spec(
        "审计这些附件", llm=MockLLM({"do": ["{}"]}), attachments=[src, req]
    )
    root = tmp_path / "m"
    refs, _ = nl.stage_files(files, root / "workspace" / "_attachments")
    spec.files = refs
    state = run(
        root,
        spec,
        llm=MockLLM(
            {
                "planner": [_code_planner()],
                "worker": [json.dumps({"kind": "finish", "summary": "复核完成"})],
            }
        ),
        assets=AssetSnapshot.freeze(
            ROOT / "pa" / "core" / "prompts",
            [ROOT / "assets" / "skills", ROOT / "assets" / "playbooks"],
            [ROOT / "assets" / "roles"],
        ),
    )
    assert state.finish_reason == "completed", state.finish_summary
    # file.scan 线索：两个附件都有（key/target 用 basename）
    scan_clues = [c for c in state.clues if c.kind == "file.scan"]
    assert {c.key for c in scan_clues} == {"app.py", "requirements.txt"}
    # findings：AWS key(high) + pickle(med) + CVE(med)，已裁决
    claims = " ".join(f.claim for f in state.findings)
    assert "AWS 访问密钥" in claims and "pickle" in claims and "CVE-2023-32681" in claims
    assert state.adjudications, "findings 必须被 JEV 裁决"
    # cred 线索落账（报告会脱敏）
    assert any(c.kind == "cred" for c in state.clues)
    # 报告：状态与脱敏
    text = render_report(state, StoreView(root))
    assert "AKIAIOSFODNN7EXAMPLE" not in text, "密钥明文不得进报告"
    assert "CVE-2023-32681" in text


def test_resume_does_not_rescan(tmp_path, monkeypatch):
    """bootstrap 扫描只在引导时执行一次；resume 重放不重复落账。"""
    monkeypatch.setenv("SECPT_LLM_MOCK", json.dumps({"do": ['{"items": []}']}))
    src = tmp_path / "app.py"
    src.write_text("x = 1\n", encoding="utf-8")
    files, _ = nl.collect_files([src])
    spec, _ = nl.build_spec(
        "审计附件", llm=MockLLM({"do": ["{}"]}), attachments=[src]
    )
    root = tmp_path / "m"
    refs, _ = nl.stage_files(files, root / "workspace" / "_attachments")
    spec.files = refs
    run(
        root,
        spec,
        llm=MockLLM(
            {
                "planner": [_code_planner()],
                "worker": [json.dumps({"kind": "finish", "summary": "ok"})],
            }
        ),
        assets=AssetSnapshot.freeze(ROOT / "pa" / "core" / "prompts", [ROOT / "assets" / "skills"]),
    )
    n_events = (root / "events.jsonl").read_text().count("\n")
    second = run(root, llm=MockLLM({}), assets=AssetSnapshot.freeze(
        ROOT / "pa" / "core" / "prompts", [ROOT / "assets" / "skills"]
    ))
    assert (root / "events.jsonl").read_text().count("\n") == n_events
    assert second.finished
