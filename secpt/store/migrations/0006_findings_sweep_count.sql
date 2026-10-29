-- M5-31 迁移：findings 增 sweep_count 列（裁判悬置上限的可复算原料）。
-- 语义：该 claim 已被 sweep 处理过的轮数。真实 run 实测一条 claim 被连扫 27 轮
-- 全悬置（每步一次重放/查账，纯烧）——上限之后 sweep 跳过，出场清单据此提示
-- "裁判已无法自动判真"，把死循环暴露给人。
-- 默认 0 兼容旧行；verified/rejected 终态行不再被 sweep 摸到，列值冻结。
ALTER TABLE findings ADD COLUMN sweep_count INTEGER NOT NULL DEFAULT 0;
