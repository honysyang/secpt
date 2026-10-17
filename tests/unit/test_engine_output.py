"""B3：大输出落盘后可发现性（头+尾+路径）测试。"""

from __future__ import annotations

import asyncio

from secpt.runtime.engine import RunConfig, make_tool_fns


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_large_output_head_tail_and_path(tmp_path):
    (tmp_path / "ws").mkdir()  # engine 的 work_dir=workspace/ws，由 job 在 run 时创建
    cfg = RunConfig(
        job_id="b3",
        objective="输出测试",
        system_extra="t",
        workspace=tmp_path,
        max_steps=5,
        max_wall_s=20,
        bash_timeout_s=20,
    )
    fns = make_tool_fns(cfg)
    out = _run(fns["bash"]({"command": "python3 -c \"print('A'*9000)\""}))
    assert "outputs/bash-1.log" in out  # 完整内容落盘且路径可发现
    assert out.count("A") > 7000  # 头 + 尾都在（中间省略）
    assert out.rstrip().endswith("A")  # 尾部保留：扫描结果末尾的关键命中不丢
    saved = (tmp_path / "ws" / "outputs" / "bash-1.log").read_text(encoding="utf-8")
    assert len(saved.rstrip()) == 9000
