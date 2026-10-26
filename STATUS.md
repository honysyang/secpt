# STATUS — secpt v0 实现状态

三态：✅ 已验收（附测试函数名）/ ⚠️ 部分实现或有限制 / ❌ 未实现。
每行 ✅ 的测试必须真实存在于 `tests/`，由 `tests/test_status.py` 保证。

## Part 1：五处地基修正

| 条目 | 状态 | 验收测试 |
| --- | --- | --- |
| 1.1 completed 仅看验收达成 | ✅ | `test_completed_ignores_remaining_queue` |
| 1.2 退出码 4 命名 halted | ✅ | `test_exit_codes_four_terminals` |
| 1.3 derive_stats 移进 scheduler.round | ✅ | `test_advisory_metric_includes_same_round_failure` |
| 1.4 advisory occurrences 单事件可重放 | ✅ | `test_advisory_occurrences_single_event_replayable` |
| 1.5 README 已知限制/退出码/术语 | ✅ | `test_readme_lists_known_limitations_exit_codes_and_terms` |

## Part 2：secpt do 门面

| 条目 | 状态 | 验收测试 |
| --- | --- | --- |
| 端口从句展开 → targets | ✅ | `test_port_clause_expansion` |
| 无目标退出 1 不建目录 | ✅ | `test_do_without_targets_exits_1_and_no_dir` |
| 编造 evidence 丢弃不报错 | ✅ | `test_fabricated_evidence_dropped_not_fatal` |
| --dry-run / 未确认零写入 | ✅ | `test_dry_run_and_unconfirmed_write_nothing` |
| do 端到端（解析→执行→退出码） | ✅ | `test_do_runs_mission_end_to_end` |

## Part 3：watch / --follow 观测

| 条目 | 状态 | 验收测试 |
| --- | --- | --- |
| render_line 逐字节确定 | ✅ | `test_render_line_byte_deterministic` |
| watch 只读（sha/行数不变）+ finished 退出码 | ✅ | `test_watch_readonly_and_finished_exit_code` |
| 追加事件 1s 内渲染 | ✅ | `test_watch_renders_appended_events_within_1s` |
| watch 退出码与 run 一致 | ✅ | `test_watch_plateau_exit_code_matches_run` |

## 补充裁定（append-only / 防扩张 / 证据类型结构化）

| 条目 | 状态 | 验收测试 |
| --- | --- | --- |
| events.jsonl 只追加不原地改写 | ✅ | `test_event_log_is_append_only` |
| LLM 不得借验收扩张 scope | ✅ | `test_llm_cannot_expand_scope` |
| clue.kind 点分枚举命中工具白名单 | ✅ | `test_clue_kinds_match_tool_whitelist` |
| kind 构造函数与白名单同源 | ✅ | `test_kind_constructors_are_the_whitelist_source` |
| probe value 与 kind 同源派生 | ✅ | `test_probe_kind_splits_from_constructor_not_literal` |
| 同族证据互为任一即可（probe.open/closed 不互锁） | ✅ | `test_exclusive_groups_any_of_within_family_and_across_families`、`test_criteria_met_treats_exclusive_family_as_any_of` |
| CJK 紧邻目标可提取（扫描aimohu.com） | ✅ | `test_targets_adjacent_to_cjk_extracted` |
| 怪异自然语言格式矩阵（全角/混排等 7 例） | ✅ | `test_weird_natural_language_formats` |
| 对抗性非目标不误提取（版本号/非法 CIDR） | ✅ | `test_adversarial_non_targets_not_extracted` |
| do 拒绝占用目录（拒绝旧账本静默续跑） | ✅ | `test_do_refuses_existing_ledger` |
| 网关门 URL 目标规范化（http://host/ → host:80） | ✅ | `test_scope_check_canonicalizes_url` |
| ApiLLM 端点自动补 /chat/completions 并报 HTTP 状态 | ⚠️ | 真实联调 aimohu 验证（DeepSeek 端到端 9 项验收起草），无独立测试 |
| token 计费走真实 usage（ApiLLM→charge 链路） | ✅ | `test_charge_prefers_real_usage_over_estimate`、`test_api_llm_records_usage_from_response`、`test_token_budget_terminates_budget_tokens` |
| dangling_dep 置 blocked(I3) 不滞留 pending | ✅ | `test_dangling_dep_blocked_with_i3_not_pending` |
| 合法 deps 一律丢弃、任务全部并列 | ✅ | `test_resolvable_deps_dropped_tasks_parallel` |
| 队列排空立即 plateau(no_ready)，不等 8 轮 | ✅ | `test_empty_queue_settles_immediately` |
| 验收三要素匹配 (evidence, target, path) | ✅ | `test_acceptance_requires_target_binding`、`test_acceptance_path_matching` |
| 无对象证据类型（shell.ok/shell.error/fs.result）禁作验收 | ✅ | `test_gen_acceptance_drops_objectless_kinds` |
| action.finished payload 带 tool（渲染不再显示指纹） | ✅ | `test_action_finished_payload_carries_tool` |
| 空轮（零调用）不写 budget.charged 结算 | ✅ | `test_charge_skips_settle_when_round_inactive` |
| 意图理解 + 评估维度（do 立面 → planner 简报） | ✅ | `test_gen_acceptance_intent_and_dimensions` |
| 笼统验收项拒绝（须指明目标/端口/路径）+ 同 evidence 去重 | ✅ | `test_gen_acceptance_rejects_vague_and_duplicate_items` |
| 路径 token 不误当主机；http 证据绑 path、probe 不绑 | ✅ | `test_gen_acceptance_path_tokens_not_hosts` |
| do 确认表 = 意图/维度/规划草案（验收项不再对用户罗列，内部仍作收口合同） | ✅ | `test_do_preview_shows_intent` |
| 规划草案进 planner 简报（## 规划草案；空草案逐字节不变） | ✅ | `test_gen_acceptance_plan_sketch_into_brief` |
| 判据同族匹配（猜错极性也算「已探测有结论」）+ 任务作用域 | ✅ | `test_criteria_family_match_and_task_scope`、`test_wrong_guess_criterion_task_still_succeeds` |
| probe 验收端口级绑定（443 不替 80 交差） | ✅ | `test_probe_port_level_acceptance` |
| worker 信息增益规则（(kind,value) 防 catch-all 烧步） | ✅ | `test_worker_novelty_stops_catch_all` |
| 敏感路径确定性 finding 探测（.git/config 等，JEV 裁决） | ✅ | `test_sensitive_finding_rules`、`test_sensitive_finding_lands_and_adjudicates` |
| token 预算按真实联调校准（12k/分钟） | ✅ | `test_budget_calibrated_to_real_usage` |
| 仓库根 .env 自动载入（不再要求先 source） | ✅ | `test_dotenv_load_fills_without_overriding` |
| 无斜杠路径名引用（robots.txt 路径）不误当越界目标 | ✅ | `test_gen_acceptance_bare_path_word_not_stray_target` |
| report 验收判定与主循环同一出口（acceptance_hit），消除「收口说 3/6、报告说 0/6」矛盾 | ✅ | `test_report_acceptance_matches_loop_verdict` |
| action.started 落账 expect/rationale（worker 思考链可读） | ✅ | `test_action_started_carries_expect_rationale` |
| `secpt log` 可读执行日志（任务/计划/执行/收口叙事；确定性；只读） | ✅ | `test_log_readable_and_readonly` |

## 已知 ⚠️ 项

| 条目 | 状态 | 说明 |
| --- | --- | --- |
| 长期记忆（跨 mission 资产） | ⚠️ | `skills/` 为人工静态目录，无经验写回、无跨客户隔离 |
| budget.seconds 口径 | ⚠️ | 墙钟结算，中断后不可复现，仅作保险丝 |

## 未实现 ❌

| 条目 | 状态 | 说明 |
| --- | --- | --- |
| decide 主控回合 | ❌ | CONTROL_PLANE 的 core/decide 全局重规划回合未实现，v0 为 planner-driven |
| 评测基线 | ❌ | 无回归评测集，模型/提示词改动效果无量化对照 |
| 策略数据化与版本化 | ❌ | 范围与策略来自 mission.json 文本，无策略库、无版本管理 |
