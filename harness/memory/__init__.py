"""记忆模块：短期对话历史 + 长期向量存储。"""

from harness.memory.store import InMemoryMemory, MemoryRecord

__all__ = ["InMemoryMemory", "MemoryRecord"]

# SQLiteMemory 延迟导入（避免 sqlite3 依赖问题）
def __getattr__(name: str):
    if name == "SQLiteMemory":
        from harness.memory.store import SQLiteMemory
        return SQLiteMemory
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
