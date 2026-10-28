-- 0002：双图表 graph_nodes / graph_edges（W2-2 pilot，DDL 原样照搬施工蓝本 §5 L505-515）
-- 版本：迁移后 PRAGMA user_version = 2。无外键（图是事实投影，允许先于小票落键）。
CREATE TABLE graph_nodes (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('asset','hypo','step','finding')),
  key TEXT NOT NULL, props_json TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1, updated_at INTEGER NOT NULL
);
CREATE TABLE graph_edges (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
  src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1, updated_at INTEGER NOT NULL
);
CREATE INDEX idx_graph_nodes_task ON graph_nodes(task_id);
CREATE INDEX idx_graph_edges_task ON graph_edges(task_id);
