"""图状态黑板：用图结构表达实体关系。

借鉴 ARTEX Exploration Graph 设计：
- 节点：资产、发现、任务、结论
- 边：关系（属于、导致、依赖）
- 支持图查询、路径分析、子图提取
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class NodeType(StrEnum):
    """节点类型。"""

    ASSET = "asset"  # 资产（文件、API、服务等）
    FINDING = "finding"  # 发现（漏洞、问题等）
    TASK = "task"  # 任务
    CONCLUSION = "conclusion"  # 结论
    AGENT = "agent"  # 智能体


class EdgeType(StrEnum):
    """边类型。"""

    BELONGS_TO = "belongs_to"  # 属于
    DEPENDS_ON = "depends_on"  # 依赖
    CAUSES = "causes"  # 导致
    RELATED_TO = "related_to"  # 相关
    DISCOVERED_BY = "discovered_by"  # 发现者
    MITIGATES = "mitigates"  # 缓解
    SUPPORTS = "supports"  # 支持
    HAS_VULNERABILITY = "has_vulnerability"  # 存在漏洞


@dataclass
class GraphNode:
    """图节点。"""

    id: str
    type: NodeType
    label: str
    properties: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    created_by: str = "system"


@dataclass
class GraphEdge:
    """图边。"""

    id: str
    source: str  # 源节点 ID
    target: str  # 目标节点 ID
    type: EdgeType
    properties: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


@dataclass
class GraphQuery:
    """图查询条件。"""

    node_type: NodeType | None = None
    node_label_contains: str | None = None
    edge_type: EdgeType | None = None
    depth: int = 1  # 遍历深度
    limit: int = 100


class GraphBlackboard:
    """图黑板：用图结构存储和查询状态。

    优势：
    - 表达实体间复杂关系
    - 支持路径分析（A 导致 B，B 影响 C）
    - 子图提取（只看某个模块的状态）
    """

    def __init__(self) -> None:
        """初始化图黑板。"""
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[str, GraphEdge] = {}
        self._node_edges: dict[str, set[str]] = {}  # node_id -> edge_ids
        self._counter = 0

    def _next_id(self, prefix: str) -> str:
        """生成唯一 ID。"""
        self._counter += 1
        return f"{prefix}_{self._counter}"

    # ==================== 节点操作 ====================

    def add_node(
        self,
        type: NodeType,
        label: str,
        properties: dict[str, Any] | None = None,
        created_by: str = "system",
        node_id: str | None = None,
    ) -> GraphNode:
        """添加节点。

        Args:
            type: 节点类型
            label: 节点标签
            properties: 属性字典
            created_by: 创建者
            node_id: 可选指定 ID

        Returns:
            创建的节点
        """
        if node_id is None:
            node_id = self._next_id("node")

        node = GraphNode(
            id=node_id,
            type=type,
            label=label,
            properties=properties or {},
            created_by=created_by,
        )
        self._nodes[node_id] = node
        self._node_edges[node_id] = set()
        return node

    def get_node(self, node_id: str) -> GraphNode | None:
        """获取节点。"""
        return self._nodes.get(node_id)

    def update_node(
        self,
        node_id: str,
        label: str | None = None,
        properties: dict[str, Any] | None = None,
    ) -> GraphNode | None:
        """更新节点。"""
        if node := self._nodes.get(node_id):
            if label is not None:
                node.label = label
            if properties is not None:
                node.properties.update(properties)
            node.updated_at = time.time()
        return node

    def remove_node(self, node_id: str) -> bool:
        """删除节点及其关联边。"""
        if node_id not in self._nodes:
            return False

        # 删除关联边
        for edge_id in list(self._node_edges.get(node_id, set())):
            self.remove_edge(edge_id)

        del self._nodes[node_id]
        del self._node_edges[node_id]
        return True

    # ==================== 边操作 ====================

    def add_edge(
        self,
        source: str,
        target: str,
        type: EdgeType,
        properties: dict[str, Any] | None = None,
        edge_id: str | None = None,
    ) -> GraphEdge | None:
        """添加边（连接两个节点）。

        Args:
            source: 源节点 ID
            target: 目标节点 ID
            type: 边类型
            properties: 属性
            edge_id: 可选指定 ID

        Returns:
            创建的边，节点不存在时返回 None
        """
        if source not in self._nodes or target not in self._nodes:
            return None

        if edge_id is None:
            edge_id = self._next_id("edge")

        edge = GraphEdge(
            id=edge_id,
            source=source,
            target=target,
            type=type,
            properties=properties or {},
        )
        self._edges[edge_id] = edge
        self._node_edges[source].add(edge_id)
        self._node_edges[target].add(edge_id)
        return edge

    def get_edge(self, edge_id: str) -> GraphEdge | None:
        """获取边。"""
        return self._edges.get(edge_id)

    def remove_edge(self, edge_id: str) -> bool:
        """删除边。"""
        if edge := self._edges.get(edge_id):
            self._node_edges[edge.source].discard(edge_id)
            self._node_edges[edge.target].discard(edge_id)
            del self._edges[edge_id]
            return True
        return False

    # ==================== 查询操作 ====================

    def query_nodes(self, query: GraphQuery) -> list[GraphNode]:
        """查询节点。

        Args:
            query: 查询条件

        Returns:
            匹配的节点列表
        """
        results = []
        for node in self._nodes.values():
            if query.node_type and node.type != query.node_type:
                continue
            if query.node_label_contains and query.node_label_contains.lower() not in node.label.lower():
                continue
            results.append(node)
            if len(results) >= query.limit:
                break
        return results

    def get_neighbors(
        self,
        node_id: str,
        edge_type: EdgeType | None = None,
        direction: Literal["out", "in", "both"] = "both",
    ) -> list[GraphNode]:
        """获取邻居节点。

        Args:
            node_id: 中心节点
            edge_type: 可选边类型过滤
            direction: out=出边, in=入边, both=双向

        Returns:
            邻居节点列表
        """
        if node_id not in self._nodes:
            return []

        neighbors = []
        for edge_id in self._node_edges.get(node_id, set()):
            edge = self._edges[edge_id]
            if edge_type and edge.type != edge_type:
                continue

            if direction in ("out", "both") and edge.source == node_id:
                if neighbor := self._nodes.get(edge.target):
                    neighbors.append(neighbor)
            elif direction in ("in", "both") and edge.target == node_id:
                if neighbor := self._nodes.get(edge.source):
                    neighbors.append(neighbor)

        return neighbors

    def get_subgraph(self, center_node_id: str, depth: int = 2) -> dict:
        """提取子图。

        Args:
            center_node_id: 中心节点
            depth: 遍历深度

        Returns:
            {"nodes": [...], "edges": [...]}
        """
        if center_node_id not in self._nodes:
            return {"nodes": [], "edges": []}

        visited_nodes = set()
        visited_edges = set()
        queue = [(center_node_id, 0)]

        while queue:
            node_id, current_depth = queue.pop(0)
            if node_id in visited_nodes or current_depth > depth:
                continue

            visited_nodes.add(node_id)
            for edge_id in self._node_edges.get(node_id, set()):
                visited_edges.add(edge_id)
                edge = self._edges[edge_id]
                next_node = edge.target if edge.source == node_id else edge.source
                queue.append((next_node, current_depth + 1))

        return {
            "nodes": [self._nodes[nid] for nid in visited_nodes],
            "edges": [self._edges[eid] for eid in visited_edges],
        }

    def find_path(
        self,
        source_id: str,
        target_id: str,
        max_depth: int = 5,
    ) -> list[GraphEdge] | None:
        """查找两节点间的路径（BFS）。

        Args:
            source_id: 源节点
            target_id: 目标节点
            max_depth: 最大深度

        Returns:
            路径上的边列表，无路径返回 None
        """
        if source_id not in self._nodes or target_id not in self._nodes:
            return None

        # BFS
        queue = [(source_id, [], 0)]
        visited = set()

        while queue:
            node_id, path, depth = queue.pop(0)
            if node_id == target_id:
                return path
            if depth >= max_depth or node_id in visited:
                continue

            visited.add(node_id)
            for edge_id in self._node_edges.get(node_id, set()):
                edge = self._edges[edge_id]
                next_node = edge.target if edge.source == node_id else edge.source
                if next_node not in visited:
                    queue.append((next_node, path + [edge], depth + 1))

        return None

    # ==================== 统计 ====================

    def get_stats(self) -> dict[str, Any]:
        """获取图统计。"""
        type_counts: dict[str, int] = {}
        for node in self._nodes.values():
            type_counts[node.type] = type_counts.get(node.type, 0) + 1

        return {
            "total_nodes": len(self._nodes),
            "total_edges": len(self._edges),
            "node_types": type_counts,
        }
