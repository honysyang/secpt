"""standalone 摘要 worker：补偿处理 compaction/pending 残留请求。

引擎非阻塞 compaction 崩溃退出时，pending 队列里可能残留未完成的摘要请求
（resume 后的下一次 run 也会自动兜底扫一次）。本脚本独立处理：

    secpt worker summarize            # 扫描全部 job 的 pending 请求
    secpt worker summarize <job_id>   # 只处理指定 job

设计同引擎内 worker：调 get_summarizer → 写 <k>.summary.md → 发
compaction_completed + legacy compaction 事件（watch 兼容）。
"""

from __future__ import annotations

import asyncio


def run_summarize_worker(job_id: str | None = None) -> int:
    """扫描并补全摘要请求。返回退出码（0=成功）。"""
    from secpt.core.context.compaction import complete_request_summary, read_pending_requests
    from secpt.core.context.summarize import get_summarizer
    from secpt.core.job.event import EventSink
    from secpt.core.job.resume import jobs_dir

    base = jobs_dir()
    if job_id:
        targets = [job_id]
    else:
        targets = (
            [d.name for d in sorted(base.iterdir()) if (d / "compaction" / "pending").is_dir()]
            if base.is_dir()
            else []
        )
    if not targets:
        print("[summarize-worker] 无待处理摘要请求")
        return 0

    total = sum(len(read_pending_requests(base, jid)) for jid in targets)
    if not total:
        print("[summarize-worker] 无待处理摘要请求")
        return 0

    print(f"[summarize-worker] 待处理 {total} 个请求（{len(targets)} 个 job）")

    async def _drain() -> int:
        done = 0
        for jid in targets:
            reqs = read_pending_requests(base, jid)
            if not reqs:
                continue
            # 单一事件循环内完成全部请求（共享 httpx 客户端不能跨 loop）
            from secpt.cli.engine_runner import _prod_llm

            summarizer = get_summarizer(_prod_llm())
            # store=None：补偿路径只补事件流与摘要文件，成本记账由事件流 tokens 承载
            sink = EventSink(jid, base, store=None)
            for path, req in reqs:
                await complete_request_summary(path, req, summarizer, sink)
                done += 1
                print(f"[summarize-worker] {jid} 摘要 #{req.get('k')} 完成")
        return done

    done = asyncio.run(_drain())
    print(f"[summarize-worker] 完成 {done}/{total}")
    return 0
