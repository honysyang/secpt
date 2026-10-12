"""intel 的 Engine 工具：observe_asset / record_fact / recall。

装配方式与 report_finding 相同（profile tools 声明即装配）——数据驱动，零执行器。
工具契约体现"程序算关系，LLM 只交观察"：observe_asset 只做原始提交，
归一化/去重/父子全在 IntelStore。
"""

from __future__ import annotations

from secpt.intel.store import ASSET_TYPES, IntelStore

OBSERVE_SCHEMA = {
    "type": "object",
    "properties": {
        "asset_type": {"type": "string", "enum": list(ASSET_TYPES)},
        "value": {"type": "string", "description": "原始观察值（URL/host:port/IP 等，无需归一化）"},
        "parent": {"type": "string", "description": "可选：父资产 value（建立父子关系）"},
        "parent_type": {"type": "string", "enum": list(ASSET_TYPES)},
    },
    "required": ["asset_type", "value"],
}

RECORD_SCHEMA = {
    "type": "object",
    "properties": {
        "node_type": {"type": "string", "enum": ["fact", "finding", "hint", "goal", "intent"]},
        "content": {
            "type": "string",
            "description": "观察内容：事实/发现/线索/失败记录（否定同样有价值）",
        },
        "related": {
            "type": "integer",
            "description": "可选：相关节点 id（建立 derived_from 血缘）",
        },
        "assets": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "可选：关联资产 id 列表（锚点，计入覆盖度）",
        },
    },
    "required": ["node_type", "content"],
}

RECALL_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "检索词（事实/发现/历史线索全文检索）"},
    },
    "required": ["query"],
}

TRACES_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "检索词（跨任务执行过程里的响应片段）"},
    },
    "required": ["query"],
}

INTEL_TOOL_DOCS = (
    "- observe_asset(asset_type, value, parent?, parent_type?)  提交资产观察"
    "（域名/IP/服务/端点；程序自动去重建关系）\n"
    "- record_fact(node_type, content, related?, assets?)  记录事实/发现/线索/"
    "失败尝试（否定结论同样有价值，防重复踩坑）\n"
    "- recall(query)  联想检索：图记忆 + 跨任务执行过程（命中标记 #图/🔗联想/≣过程）\n"
    "- search_traces(query)  专查其他任务的过程日志（哪一步见过类似响应）"
)


def make_intel_tools(store: IntelStore | None = None, *, job_id: str, box: dict | None = None):
    """返回 {name: (schema, fn)}。每个 fn 独立容错（记忆层失败不阻断主链路）。

    box: 步号共享盒（由 StepTracker 每步更新），record_fact 的证据步指针用它。
    """
    st = store or IntelStore()
    box = box if box is not None else {"n": 0}

    async def observe_asset(args: dict) -> str:
        try:
            parent_id = None
            if args.get("parent") and args.get("parent_type"):
                parent_id = st.find_asset(args["parent_type"], args["parent"])
                if parent_id is None:
                    parent_id = st.observe_asset(args["parent_type"], args["parent"], job_id=job_id)
            aid = st.observe_asset(
                args["asset_type"], args["value"], parent_id=parent_id, job_id=job_id
            )
            return f"OK: asset#{aid}（type={args['asset_type']}, value={args['value'][:80]}）"
        except (ValueError, KeyError) as e:
            return f"Error: {e}"

    async def record_fact(args: dict) -> str:
        try:
            nid = st.add_node(
                args["node_type"],
                args["content"],
                job_id=job_id,
                step=box["n"],
                anchor_assets=[int(a) for a in (args.get("assets") or [])],
            )
            rel = args.get("related")
            if rel:
                st.add_edge(int(rel), nid, "derived_from")
            return f"OK: node#{nid}（{args['node_type']}）已记录"
        except (ValueError, KeyError) as e:
            return f"Error: {e}"

    async def recall(args: dict) -> str:
        """联想召回：intel 图 + 跨 job 执行过程（过程记忆排在图记忆后）。"""
        q = str(args.get("query") or "")
        lines: list[str] = []
        try:
            hits = st.recall_graph(q, limit=8)
            for h in hits[:8]:
                tag = "🔗" if h.get("associated") else "#"
                lines.append(f"{tag}{h['id']} [{h['type']}] {h['content'][:160]}")
        except Exception as e:
            return f"Error: {e}"
        try:
            from secpt.core.job.store import JobStore

            for t in JobStore().search_traces(q, limit=4, exclude_job=job_id)[:4]:
                lines.append(f"≣{t['job_id']}:{t['seq']} [{t['type']}] {t['preview'][:160]}")
        except Exception:
            pass
        if not lines:
            return "（无匹配记忆）"
        return "\n".join(lines)

    async def search_traces(args: dict) -> str:
        """专查跨任务执行过程（ARTEX 原则 5：worker 互查中间日志）。"""
        q = str(args.get("query") or "")
        try:
            from secpt.core.job.store import JobStore

            traces = JobStore().search_traces(q, limit=8, exclude_job=job_id)
        except Exception as e:
            return f"Error: {e}"
        if not traces:
            return "（无匹配过程记录）"
        return "\n".join(
            f"≣{t['job_id']}:{t['seq']} [{t['type']}] {t['preview'][:180]}" for t in traces[:8]
        )

    return {
        "observe_asset": (
            {
                "type": "function",
                "function": {
                    "name": "observe_asset",
                    "description": "提交资产观察（域名/IP/服务/端点）；程序自动去重与建父子关系",
                    "parameters": OBSERVE_SCHEMA,
                },
            },
            observe_asset,
        ),
        "record_fact": (
            {
                "type": "function",
                "function": {
                    "name": "record_fact",
                    "description": "记录事实/发现/线索/失败尝试（否定结论同样有价值）；可挂血缘与资产锚点",
                    "parameters": RECORD_SCHEMA,
                },
            },
            record_fact,
        ),
        "recall": (
            {
                "type": "function",
                "function": {
                    "name": "recall",
                    "description": "联想检索：跨任务历史事实/发现 + 执行过程",
                    "parameters": RECALL_SCHEMA,
                },
            },
            recall,
        ),
        "search_traces": (
            {
                "type": "function",
                "function": {
                    "name": "search_traces",
                    "description": "跨任务执行过程检索（其他 job 哪一步见过类似响应）",
                    "parameters": TRACES_SCHEMA,
                },
            },
            search_traces,
        ),
    }


def maybe_attach_intel(
    extra_tools: dict, names: list[str], job_id: str, box: dict | None = None
) -> dict:
    """names 声明 intel 即装配三件套（与 report_finding 同一装配通道）。"""
    tools = dict(extra_tools)
    if any(
        t in names for t in ("intel", "observe_asset", "record_fact", "recall", "search_traces")
    ):
        tools.update(make_intel_tools(job_id=job_id, box=box))
    return tools
