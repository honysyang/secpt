"""事实层：sqlite 黑板（小票/findings/双图/水位线）的唯一读写区。

写入口纪律：任何模块要写这些表必须经由本包；append-only 由 schema.sql
中的触发器在数据库层面强制（越权直写原地报错，不是代码自觉）。
"""

from secpt.store.migrate import connect, ensure_schema, schema_version

__all__ = ["connect", "ensure_schema", "schema_version"]
