"""黑板体系：多智能体共享状态的三层实现。

模块划分：
- base:         基础黑板（读写、事件订阅、命名空间、乐观锁）
- hierarchical: 分层黑板（Global/Domain/Local、TTL、关键信息保护）
- graph:        图黑板（节点/边、依赖追踪、子图查询）

统一从包根导入：

    from runtime.blackboard import (
        Blackboard,              # 基础黑板
        HierarchicalBlackboard,  # 分层黑板（推荐用于多智能体协作）
        GraphBlackboard,         # 图黑板（用于依赖追踪与结论溯源）
    )
"""

from runtime.blackboard.base import Blackboard, BlackboardAgent, BlackboardEntry, BlackboardEvent
from runtime.blackboard.graph import EdgeType, GraphBlackboard, GraphEdge, GraphNode, GraphQuery, NodeType
from runtime.blackboard.hierarchical import (
    BlackboardError,
    BlackboardLayer,
    CapacityExceededError,
    ConflictError,
    HierarchicalBlackboard,
    PermissionDeniedError,
    SmartBlackboardAgent,
    TTLBlackboardEntry,
)

__all__ = [
    # 基础黑板
    "Blackboard",
    "BlackboardAgent",
    "BlackboardEntry",
    "BlackboardEvent",
    # 分层黑板
    "BlackboardError",
    "BlackboardLayer",
    "CapacityExceededError",
    "ConflictError",
    "HierarchicalBlackboard",
    "PermissionDeniedError",
    "SmartBlackboardAgent",
    "TTLBlackboardEntry",
    # 图黑板
    "EdgeType",
    "GraphBlackboard",
    "GraphEdge",
    "GraphNode",
    "GraphQuery",
    "NodeType",
]
