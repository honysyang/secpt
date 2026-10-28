-- 0003：经验表 experience（W3-1 经验库激活，DDL 原样照搬施工蓝本 §5 L517-527）
-- 版本：迁移后 PRAGMA user_version = 3。
-- 纪律：经验一经沉淀永不满血搬家——DELETE 触发器拒掉（append-only 语义）；
-- UPDATE 放行留给 M3 判票（useful_count 计票），M2 阶段无人写它。
CREATE TABLE experience (                  -- 长期记忆（M2 激活）：谁都能读/唯 LLMB 写
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  environment TEXT NOT NULL,
  verify_state TEXT NOT NULL,
  useful_count INTEGER NOT NULL DEFAULT 0,
  summary   TEXT NOT NULL,
  command   TEXT,
  raw_ref   TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_experience_env ON experience(environment);
CREATE TRIGGER trg_experience_no_delete
  BEFORE DELETE ON experience
BEGIN
  SELECT RAISE(ABORT, 'E_APPEND_ONLY: experience 一经沉淀不许删除');
END;
