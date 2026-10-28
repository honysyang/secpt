-- M3-3 迁移：observations 表（蓝本 §5 L496-503 DDL 原样，一条不增不减）。
-- 定位：流量真相 = LLM 小票之外的客观事件流（§3.2 双通道证据的通道 B）。
CREATE TABLE observations (                -- 流量真相：LLM 小票之外的客观事件流
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL, run_id TEXT NOT NULL,
  channel TEXT NOT NULL,                    -- http | tcpdump
  meta_json TEXT NOT NULL,
  snapshot_ref TEXT NOT NULL,               -- 原始报文文件指针
  ts INTEGER NOT NULL
);
