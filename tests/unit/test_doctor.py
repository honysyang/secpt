"""G：doctor 环境预检（只测检查函数，不依赖真实 docker/网络）。"""

from __future__ import annotations


def test_run_checks_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dk-test")
    from secpt.cli.cmd_doctor import run_checks

    results = run_checks()
    names = [name for name, _ok, _d in results]
    assert any("python" in n for n in names)
    assert any("模型凭证" in n for n in names)
    assert any("var/ 可写" in n for n in names)
    assert any("工具 curl" in n for n in names)
    assert all(isinstance(ok, bool) and isinstance(d, str) for _n, ok, d in results)
    # 模型凭证应通过（key 已设置、resolve 成功）
    cred = next(r for r in results if r[0] == "模型凭证")
    assert cred[1] is True


def test_doctor_cmd_prints(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SECPT_VAR", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dk-test")
    from secpt.cli.cmd_doctor import doctor_cmd

    doctor_cmd()
    out = capsys.readouterr().out
    assert "检查项" in out and "模型凭证" in out
