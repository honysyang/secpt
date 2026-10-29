-- M5-11 迁移：findings 增 actor 列（R2"不同 worker 双路径"的判定原料）。
-- 语义：谁申报了这条 claim（main = 主循环，workerA/workerB = 派单工人）。
-- 默认 'main' 兼容旧行：M5 之前所有 finding 都由主循环收口落账。
ALTER TABLE findings ADD COLUMN actor TEXT NOT NULL DEFAULT 'main';
