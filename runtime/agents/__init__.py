"""多智能体协作模块。"""

from runtime.agents.base import AgentResult, SubAgent
from runtime.agents.collaboration import CollaborationResult, Debate, Parallel, Router
from runtime.agents.roles import (
    BUILTIN_ROLES,
    DuplicateRoleError,
    RoleError,
    RoleNotFoundError,
    RoleRegistry,
    RoleSpec,
)

__all__ = [
    "BUILTIN_ROLES",
    "AgentResult",
    "CollaborationResult",
    "Debate",
    "DuplicateRoleError",
    "Parallel",
    "RoleError",
    "RoleNotFoundError",
    "RoleRegistry",
    "RoleSpec",
    "Router",
    "SubAgent",
]
