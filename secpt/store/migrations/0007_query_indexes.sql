-- M5-34 迁移：查询路径补索引（三轮真实 run 的账本规模下 sweep/打包/审计窗
-- 全部退化成全表扫描）。逐条对账：
-- ① tickets(raw_ref)：R1 取票按 (raw_ref, task_id) 查——每步 sweep 每条 claim
--    一次，全表扫描是 O(票数 × sweep 轮数)；
-- ② plans(task_id, status)：注意力包每步 render active 规划 + 出场第 5 验；
-- ③ findings(task_id, verify_state)：sweep 只摸 claimed、报告只收 verified；
-- ④ experience(task_id, environment, raw_ref)：判票 promote/decay 按此锚查；
-- ⑤ observations(task_id)：R0 obs 锚判零 + 检查窗。
-- 索引只加速读路径，不动任何语义；append-only 触发器不受影响。
CREATE INDEX idx_tickets_anchor ON tickets(task_id, raw_ref);
CREATE INDEX idx_plans_task_status ON plans(task_id, status);
CREATE INDEX idx_findings_task_state ON findings(task_id, verify_state);
CREATE INDEX idx_experience_anchor ON experience(task_id, environment, raw_ref);
CREATE INDEX idx_observations_task ON observations(task_id);
