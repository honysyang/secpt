"""角色体系：团队拓扑的角色定义与角色化阶段提示（frontier 角色意图的语义定义）。

参考 ARTEX 团队拓扑：目标拆解（goals）→ 角色意图（role）→ 认领互斥（claim）→ 事件回馈。
五角色闭环（角色语义与已退役的固定 DAG 编排一一对应，现改为意图表达、可并行认领）：
- recon    侦察：枚举资产/指纹/端口 → observe_asset 入图谱（无依赖，先行）
- exploit  利用：针对单个资产做定向利用（depends_on 对应 recon；每资产一条，可多 worker 并行）
- lateral  横向：凭据复用/横向移动/数据定位（depends_on fact:credential——intel 图出现
           凭据事实即解锁；fact 依赖由 deps_satisfied 确定性判定，不靠模型自觉）
- verify   复验：对 exploit 声称的 finding/flag 做独立 PoC 复现（depends_on 对应 exploit）
- report   聚合：verify 全部完成后产结构化渗透报告（depends_on 全部 verify）

实现原则：不新增渲染机制——角色化通过给 worker objective 追加
「【当前阶段：<role>】」片段实现（objective_extra 语义），
profile 渲染链路（profile/render.py）原样复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RoleSpec:
    name: str
    role_prompt: str  # 角色定位（供 planner / 人读）
    stop_condition: str  # 该角色的收尾要求（注入角色 stage）
    tools: list[str] = field(default_factory=list)  # 建议装配工具（空则沿用 profile tools）


ROLE_MAP: dict[str, RoleSpec] = {
    "recon": RoleSpec(
        name="recon",
        role_prompt="资产侦察员：摸清目标指纹、端口、目录与技术栈，不做深度利用",
        stop_condition="侦察完成（或预算耗尽）后输出 ANSWER: <侦察摘要>",
    ),
    "exploit": RoleSpec(
        name="exploit",
        role_prompt="定向利用者：针对单个资产深挖并利用，深度优先",
        stop_condition="拿到 flag 或确认漏洞后输出 ANSWER: <结论>",
    ),
    "lateral": RoleSpec(
        name="lateral",
        role_prompt=(
            "横向移动者：基于已获得的凭据/访问权做复用与扩散（内网服务、相邻主机、"
            "数据定位），不做无凭据的新突破"
        ),
        stop_condition="横向验证结论输出 ANSWER: <可达范围/数据结论>",
    ),
    "verify": RoleSpec(
        name="verify",
        role_prompt="复验员：对声称的 finding/flag 用独立 PoC 重新复现，成功才确认",
        stop_condition="复验结论输出 ANSWER: <复验结果>",
    ),
    "report": RoleSpec(
        name="report",
        role_prompt="报告聚合员：汇总已验证 finding/资产/否定记录，产出结构化报告",
        stop_condition="报告落盘后输出 ANSWER: <报告路径>",
    ),
}


def role_stage(role: str, asset_scope: str = "") -> str:
    """按角色生成阶段提示（worker objective 的追加片段；对齐 specialist 的 objective_extra）。"""
    spec = ROLE_MAP.get(role)
    if spec is None:
        return ""
    if role == "recon":
        return (
            "【当前阶段：资产侦察（recon）】\n"
            "先对目标做资产枚举：端口 / 服务 / 目录 / 技术栈。\n"
            "侦察结束前，把结构化资产清单写入文件 ws/assets.json（JSON 数组，"
            "每项含 host/port/service/tech 四个字段，没有的字段给空字符串或 null）。\n"
            "发现疑似默认口令/弱配置时，用 record_fact 以 credential:<服务> 前缀记录，"
            "供 lateral 意图复用。\n"
            "没有侦察到资产时也写一个空数组 []。"
        )
    if role == "exploit":
        target = asset_scope or "目标入口"
        return (
            f"【当前阶段：定向利用（exploit）】\n针对资产 {target} 做定向利用。\n"
            "利用成功（拿到 flag 或确认漏洞）后输出 ANSWER: <结论>。\n"
            "【硬约束】每获得一个凭据或访问权，必须立即 record_fact 记录，"
            "前缀规则：凭据用 credential:<服务>（含 username/password/token 字段）、"
            "已取得的访问路径用 access:<主机或服务>（如 access:ssh-10.0.0.5）、"
            "已确认漏洞用 vuln:<资产>:<漏洞>（含 evidence）。"
            "没有 record_fact 的凭据等于不存在——后续 lateral 意图不会被解锁。"
        )
    if role == "lateral":
        return (
            "【当前阶段：横向移动（lateral）】\n基于已记录的凭据/访问事实（先 recall "
            "fact:credential / fact:access 拉全量）做复用与扩散：同一凭据尝试其他主机/服务、"
            "已控主机上翻取配置与数据、内网可达性探测。\n"
            "不做无凭据的新突破（那是 recon/exploit 的活）；"
            "每发现一条新访问路径或敏感数据，立即 record_fact（access: / data: 前缀），"
            "让后续意图与报告可消费。\n"
            "横向验证结论输出 ANSWER: <可达范围/数据结论>。"
        )
    if role == "verify":
        return (
            "【当前阶段：复验（verify）】\n对前置意图声称的 finding/flag 用独立 PoC "
            "重新复现（新 workspace / 新请求），复验通过才确认；复现失败的给出否定结论。\n"
            "复验结论输出 ANSWER: <复验结果>。\n"
            "【硬约束】禁止 read_file 查看前置意图的旧 transcript/日志来照搬原证据——"
            "复验必须用你自己发起的全新请求得出结论（独立 PoC），旧证据只作线索不作依据。"
        )
    if role == "report":
        return (
            "【当前阶段：聚合报告（report）】\n汇总已验证 finding、资产、凭据与否定记录，"
            "生成结构化渗透报告（目标 / 侦察发现 / 漏洞与证据 / PoC 复现步骤 / 影响 / 建议）。"
        )
    return spec.stop_condition


def role_objective(role: str, asset_scope: str = "") -> str:
    """角色化客观描述（供 planner / 报告引用），无角色返回空。"""
    spec = ROLE_MAP.get(role)
    if spec is None:
        return ""
    parts = [f"【当前角色】{spec.role_prompt}"]
    if asset_scope:
        parts.append(f"【挂靠资产】{asset_scope}")
    parts.append(f"【收尾要求】{spec.stop_condition}")
    return "\n".join(parts)
