"""intel：双图记忆（0day 出在海量观察的交叉点）。

- 资产图（真值库）：节点 root_domain/subdomain/ip/service/app/endpoint，
  父子关系与去重 key 全部由**程序计算**——agent 只提交原始观察（ARTEX 的核心取舍）
- 探索图（血缘链）：goal/intent/fact/finding/hint + 边
  spawns/derived_from/yields/proves——finding 可溯源到"哪两个 fact 综合得出"
- 锚点表：exploration 节点 ↔ asset 关联 → 覆盖度可计算
  （挖洞最怕自以为测完了）

与 warroom 的关系：warroom 是作业内高速暂存（凭据/尝试去重），intel 是
跨任务持久化的图记忆；二者互补不替代。
"""

from secpt.intel.store import IntelStore

__all__ = ["IntelStore"]
