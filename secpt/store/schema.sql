-- ============================================================================
-- secpt store · 版本 1（W1-1）：六表（tasks/runs/plans/steps/tickets/findings）
--                   + audit_log（M1 即启用）
--
-- 本文件是 schema 的 baseline。此后每次结构变更都新增一个迁移文件
-- migrations/NNNN_*.sql，由 migrate.py 按 PRAGMA user_version 幂等推进；
-- 修改本文件被禁止——否则昨夜已建库的实例无法续跑（风险登记册 R4）。
--
-- 写入口纪律（重申为机制而非约定）：
--   tickets / audit_log 是 append-only，由下方触发器在数据库层面强制——
--   任何绕过 store 模块直接 UPDATE/DELETE 这些表的操作原地报错。
--   findings 允许 UPDATE（裁判要推进 verify_state），但禁止 DELETE。
--   graph_nodes/graph_edges 与 experience/observations 沿后续 migration 建立。
-- ============================================================================

CREATE TABLE tasks (
  id TEXT PRIMARY KEY,                      -- uuid4 hex
  input_text   TEXT NOT NULL,
  goal         TEXT,                        -- NULL = 对话模式
  mode         TEXT NOT NULL CHECK (mode IN ('conversation','pentest')),
  exit_kind    TEXT CHECK (exit_kind IN ('normal','abnormal','stalled')),
  created_at   INTEGER NOT NULL
);

CREATE TABLE runs (                        -- 断电续跑的锚
  id TEXT PRIMARY KEY,
  task_id      TEXT NOT NULL REFERENCES tasks(id),
  status       TEXT NOT NULL DEFAULT 'running'
               CHECK (status IN ('running','paused','interrupted','ended')),
  step_count   INTEGER NOT NULL DEFAULT 0,  -- 水位线
  config_json  TEXT NOT NULL,               -- 本次 run 参数快照
  started_at   INTEGER NOT NULL,
  ended_at     INTEGER
);

CREATE TABLE plans (
  id TEXT PRIMARY KEY,
  task_id    TEXT NOT NULL REFERENCES tasks(id),
  step_no    INTEGER NOT NULL,
  content    TEXT NOT NULL,                 -- A/B/C… 一句规划
  parent_id  TEXT,                          -- 规划重排时指向上版
  status     TEXT NOT NULL DEFAULT 'active'
             CHECK (status IN ('active','done','cancelled','failed')),
  version    INTEGER NOT NULL DEFAULT 1,
  updated_at INTEGER NOT NULL
);

CREATE TABLE steps (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  run_id  TEXT NOT NULL REFERENCES runs(id),
  step_no INTEGER NOT NULL,
  plan_refs  TEXT NOT NULL,                 -- json: [plan_id,...]
  llm_output TEXT NOT NULL,                 -- 结构化输出原文（审计）
  status     TEXT NOT NULL CHECK (status IN ('ok','schema_retry','voided','aborted')),
  started_at INTEGER,
  ended_at   INTEGER
);

CREATE TABLE tickets (                     -- 小票（append-only）
  id TEXT PRIMARY KEY,
  task_id   TEXT NOT NULL,
  run_id    TEXT NOT NULL,
  step_id   TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  args_json TEXT NOT NULL,
  exit_code INTEGER,
  summary   TEXT NOT NULL,
  raw_ref   TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE findings (                    -- 台账：claimed→verified/rejected
  id TEXT PRIMARY KEY,
  task_id   TEXT NOT NULL,
  title     TEXT NOT NULL,
  detail    TEXT NOT NULL,
  verify_state TEXT NOT NULL DEFAULT 'claimed'
       CHECK (verify_state IN ('claimed','verified','rejected')),
  method    TEXT CHECK (method IS NULL OR method IN ('rerun','dual-path','negative','oracle','manual')),
  evidence_ref TEXT NOT NULL,
  confidence REAL,
  judged_at INTEGER,
  created_at INTEGER NOT NULL
);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id   TEXT NOT NULL,
  actor     TEXT NOT NULL,                  -- main/workerA/llmc/llma/llmb/judge
  what      TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

-- ---- 常用索引（按身份列；小表不加索引防噪音） -------------------------------
CREATE INDEX idx_runs_task       ON runs(task_id);
CREATE INDEX idx_steps_run       ON steps(run_id);
CREATE INDEX idx_tickets_run     ON tickets(run_id);
CREATE INDEX idx_tickets_task    ON tickets(task_id);
CREATE INDEX idx_findings_task   ON findings(task_id);
CREATE INDEX idx_audit_task      ON audit_log(task_id);

-- ---- append-only 的机制化触发器 --------------------------------------------
-- 越权直写 sqlite 应报错，而不是靠代码自觉（失败注入测试的靶子）。
CREATE TRIGGER trg_tickets_no_update BEFORE UPDATE ON tickets
BEGIN
  SELECT RAISE(ABORT, 'E_APPEND_ONLY: tickets is append-only');
END;

CREATE TRIGGER trg_tickets_no_delete BEFORE DELETE ON tickets
BEGIN
  SELECT RAISE(ABORT, 'E_APPEND_ONLY: tickets is append-only');
END;

CREATE TRIGGER trg_findings_no_delete BEFORE DELETE ON findings
BEGIN
  SELECT RAISE(ABORT, 'E_APPEND_ONLY: findings rows are never deleted');
END;

CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON audit_log
BEGIN
  SELECT RAISE(ABORT, 'E_APPEND_ONLY: audit_log is append-only');
END;

CREATE TRIGGER trg_audit_no_delete BEFORE DELETE ON audit_log
BEGIN
  SELECT RAISE(ABORT, 'E_APPEND_ONLY: audit_log is append-only');
END;
