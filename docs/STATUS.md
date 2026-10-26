# STATUS — secpt v0 实现状态

三态：✅ 已验收（附测试函数名）/ ⚠️ 部分实现或有限制 / ❌ 未实现。
每行 ✅ 的测试必须真实存在于 `tests/`，由 `tests/test_status.py` 保证。

## v0.2：工作流正确性与报告

| 条目 | 状态 | 验收测试 |
| --- | --- | --- |
| 任务判据合同门（散文 / fs.read / shell.output 拒绝） | ✅ | `test_contract_rejects_prose_and_unregistered_criteria`、`test_mixed_draft_keeps_valid_criterion_and_flags_bad_one` |
| batch 先执行完整批、再统一判据 | ✅ | `test_batch_runs_all_actions_before_criteria` |
| 依赖失败传播为 blocked(dep_failed) | ✅ | `test_dep_failure_blocks_downstream` |
| do 永远新开 runs/N，不复用旧账本 | ✅ | `test_next_run_path_never_reuses_existing_ledger` |
| 空计划回落确定性基线（端口/HTTP/TLS） | ✅ | `test_empty_plan_falls_back_to_baseline` |
| orchestrator abandon 废止任务并阻止自动重排队 | ✅ | `test_abandon_supersedes_and_survives_requeue` |
| coach advisory 进入 orchestrator 上下文 | ✅ | `test_shadow_byte_identical_to_off_with_advisories`、`test_plan_missing_criteria_shadow_advises_enforce_blocks` |
| tried 指纹随状态事件落盘（崩溃重放一致） | ✅ | `test_die_at_step_and_resume_matches_uninterrupted` |
| HTTP 结构化 meta（headers/length/body hash/Location） | ✅ | `test_http_meta_and_dns_clues`、`test_http_meta_criterion_requires_headers` |
| DNS 工具与 dns.result 线索 | ✅ | `test_registry_and_direct_calls`、`test_http_meta_and_dns_clues` |
| 经验条目带 finish_reason/tasks_done/clues/confirmed 溯源 | ✅ | `test_distill_appends_with_origin_and_inherited` |
| 审计断链计数支持 dict 形态（不再恒 0） | ✅ | `test_distill_input_counts_broken_dicts` |
| JSON 后追加散文可解析（Extra data） | ✅ | `test_parse_json_accepts_trailing_prose` |
| 80/tcp 不再被误绑成验收路径 /tcp | ✅ | `test_gen_acceptance_tcp_suffix_is_not_a_path` |
| 报告状态机 / 验收核对矩阵 / 已排除观察 / 执行质量 | ✅ | `test_report_status_matrix_and_observation` |

## v0.3：finding 管道与产出质量

| 条目 | 状态 | 验收测试 |
| --- | --- | --- |
| worker Finish 随附 findings（引擎绑定本任务证据，无证据不提案） | ✅ | `test_finish_findings_become_jev_finding`、`test_finish_findings_skipped_without_evidence` |
| finding 稳定身份（claim/reproduce/asset_id；演进不再孤儿裁决） | ✅ | `test_stable_finding_id_across_evidence_evolution`、`test_overview_verdict_counts_follow_merged_findings` |
| proposal_key：提案事件幂等与身份分离（账本保留原始提案流） | ✅ | `test_stable_finding_id_across_evidence_evolution` |
| suspected 裁决带 unlock 补证方向（报告/log 展示） | ✅ | `test_finish_findings_become_jev_finding` |
| mission 任务上限（超限 create 整批拒绝，随 decision 落账） | ✅ | `test_guard_create_respects_mission_task_cap` |
| goal 归一化去重（换措辞的同质任务拒绝；superseded 可重建） | ✅ | `test_guard_create_dedupes_normalized_goals` |
| stats 产出质量：confirmed/100k tokens、模型解析失败率、零产出提示 | ✅ | `test_stats_three_terminals_one_row_each`、`test_stats_suggestions_fire_on_threshold_crossing` |
| 回归：log 动作配对跳过思考节点（legacy 账本 KeyError: 'fp'） | ✅ | `test_log_render_with_finish_thought` |

## v0.4：确定性安全能力与二验闭环

| 条目 | 状态 | 验收测试 |
| --- | --- | --- |
| secrules 规则引擎：缺失安全响应头（HSTS/CSP/XFO/XCTO/RP/PP，https 才查 HSTS） | ✅ | `test_header_finding_https_missing_is_low`、`test_header_finding_http_missing_is_info`、`test_header_finding_noop_cases` |
| secrules：过时 TLS 协议 / 弱套件 / 链校验失败 / 证书临期（正交四规则） | ✅ | `test_tls_findings_rules` |
| secrules：catch-all 同构检测（≥3 路径同 body_sha256 且含敏感形态） | ✅ | `test_catchall_fires_and_suppresses_sensitive`、`test_catchall_not_fired_without_sensitive_path` |
| catch-all 确认后抑制 sensitive 误报（同批内生效） | ✅ | `test_catchall_fires_and_suppresses_sensitive` |
| worker 集成：规则 finding 走 propose_finding 统一出口 | ✅ | `test_worker_proposes_header_finding` |
| 高危单证据 suspected → verify_finding 二验信号（PentAGI auditor 模式） | ✅ | `test_high_suspected_finding_triggers_verify_advisory` |
| 失败复盘进简报：last_failure（最近一次失败原因；成功不背旧包袱） | ✅ | `test_briefing_carries_last_failure` |
| worker 相线索段不可信数据标注（含空线索） | ✅ | `test_context_renders_last_failure_and_untrusted_note` |
| 报告「资产与暴露面」一节（端口/关闭/解析/端点/TLS，确定性排序） | ✅ | `test_report_asset_section` |

## v0.5：进展判定修复（真跑 thrashing 根因）

| 条目 | 状态 | 验收测试 |
| --- | --- | --- |
| 新颖性基线动作前快照（pre_kv 在落账后计算 → novel 恒 False） | ✅ | `test_batch_survives_uniform_200_site` |
| is_progress 用 out.ok 判成功（http Outcome.exit_code=200≠0 → 枚举必 needs_help） | ✅ | `test_batch_survives_uniform_200_site`、`test_batch_capped_at_8` |
| 内容新颖按主题作用域（http 按站点 / probe·tls 按 host:port / 其余全局） | ✅ | `test_worker_novelty_stops_catch_all`（早停保留）、`test_batch_capped_at_8`（多主机枚举不误停）、`test_batch_survives_uniform_200_site` |
| header finding 站点级聚合（reproduce 不含路径，同站多路径合并） | ✅ | `test_header_finding_aggregates_per_site` |

## v0.6：命令行工作流与日志完备

| 条目 | 状态 | 验收测试 |
| --- | --- | --- |
| `secpt status`：mission 家族一览（runs/终态/任务/findings/墙钟，默认扫 missions/） | ✅ | `test_status_lists_families`、`test_status_empty_and_family_root` |
| latest 语义统一：数字 runs/N 最大者胜出（根账本视为 N=1；status/resume/log/report/watch 同口径） | ✅ | `test_overview_latest_run_prefers_max_n` |
| `secpt log --flow`：流程叙事离线重放（确定性） | ✅ | `test_log_flow_flag_offline_replay` |
| log 编排决策头带每步 token 结算 | ✅ | `test_log_step_tokens_and_attempt_meta` |
| log 任务标题带尝试次数/耗时/动作数 | ✅ | `test_log_step_tokens_and_attempt_meta` |
| trace/tui/stats 接受家族目录（自动解析 latest） | ✅ | `test_family_dir_resolves_for_trace_and_stats` |
| do 确认面板展示验收合同（反转旧「不罗列」裁定：用户签的是收口合同） | ✅ | `test_do_panel_shows_acceptance` |
| do 结束打印下一步命令（log/resume/status 指引） | ✅ | `test_do_prints_next_hints` |

## v0.7：附件一等公民与 0day 研究工作流

| 条目 | 状态 | 验收测试 |
| --- | --- | --- |
| 附件收录/暂存：目录递归、符号链接跳过、单文件/总量/数量上限、确定性命名（重名跳过）、二进制检测 | ✅ | `test_collect_files_dir_recursive_and_warnings`、`test_stage_files_deterministic_names_and_binary` |
| 纯文件任务：任务文本无目标 + 有附件 → file-only mission（file.scan 验收），不再强制网络目标 | ✅ | `test_build_spec_file_only_mission`、`test_no_targets_and_no_files_still_raises` |
| filerules 密钥规则：AWS/GitHub/Slack/私钥/泛凭据，值脱敏，占位符过滤仅限 generic（AKIA…EXAMPLE 是信号不是豁免） | ✅ | `test_scan_text_secrets_and_redaction` |
| filerules 危险 sink：eval/exec/shell=True/os.system/pickle/yaml.load/verify=False/sql f-string/弱哈希/debug=True | ✅ | `test_scan_text_dangerous_sinks` |
| 依赖解析 + 本地 advisory 匹配（SECPT_ADVISORIES JSONL，introduced≤v<fixed；requirements/package.json/go.mod） | ✅ | `test_parse_deps_and_advisory_match`、`test_parse_deps_package_json_and_go_mod` |
| fs.grep：正则 + glob + ignore_case、二进制跳过、匹配数封顶、坏正则 blocked、路径逃逸仍拦 | ✅ | `test_fs_grep_matches_and_binary_skip` |
| bootstrap 附件扫描：evidence + finding.proposed（JEV 管道）+ cred 线索（报告脱敏）+ file.scan 线索（basename 锚定） | ✅ | `test_attachment_mission_end_to_end` |
| resume 不重扫（幂等：账本行数不变） | ✅ | `test_resume_does_not_rescan` |
| 附件进 worker 简报（files 段）与 flow/log 头部展示 | ✅ | `test_attachment_mission_end_to_end`（简报经 fs.grep 复核链路） |
| code 审计资产：roles/analyst.md + playbooks/pb-code.md + skills/source-review.md（数据驱动注入） | ✅ | `test_attachment_mission_end_to_end`（role=analyst 注入 + fs.grep 执行） |

## v0.8：运行分析文档（每 run 自动落盘）

| 条目 | 状态 | 验收测试 |
| --- | --- | --- |
| 任意终态自动落盘 runs/N/analysis.md（completed 与失败运行都写） | ✅ | `test_analysis_written_on_completed`、`test_analysis_written_on_plateau_with_attribution` |
| 结构：运行元信息/系统信号/优化建议/失败归因/运行过程全记录 | ✅ | `test_analysis_written_on_completed` |
| 优化建议具体到本次运行（needs_help/缓存命中/编排降级/合同门拒绝/零 finding） | ✅ | `test_analysis_written_on_plateau_with_attribution` |
| `secpt report` 一键重新生成 report.md + analysis.md | ✅ | `test_analysis_regen_via_report_cli` |
| 叙事单一事实源（analysis 的过程段 == render_log 去标题） | ✅ | `test_analysis_narrative_matches_log` |

## Part 1：五处地基修正

| 条目 | 状态 | 验收测试 |
| --- | --- | --- |
| 1.1 completed 需队列排空（验收达成 ≠ 活干完，跑空才收口） | ✅ | `test_completed_drains_queue_first` |
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
| do 重复跑自动开 runs/N（不再拒绝旧账本） | ✅ | `test_do_repeated_runs_family_runs_n` |
| 网关门 URL 目标规范化（http://host/ → host:80） | ✅ | `test_scope_check_canonicalizes_url` |
| ApiLLM 端点自动补 /chat/completions 并报 HTTP 状态 | ⚠️ | 真实联调 aimohu 验证（DeepSeek 端到端 9 项验收起草），无独立测试 |
| token 计费走真实 usage（ApiLLM→charge 链路） | ✅ | `test_charge_prefers_real_usage_over_estimate`、`test_api_llm_records_usage_from_response`、`test_token_budget_terminates_budget_tokens` |
| dangling_dep 置 blocked(I3) 不滞留 pending | ✅ | `test_dangling_dep_blocked_with_i3_not_pending` |
| 合法 deps 一律丢弃、任务全部并列 | ✅ | `test_resolvable_deps_dropped_tasks_parallel` |
| 空计划回落确定性基线（不再 0 任务 plateau） | ✅ | `test_empty_plan_falls_back_to_baseline` |
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
| tls 工具（stdlib ssl：握手/密码套件/证书字段/链校验；tls.cert/tls.error 线索） | ✅ | `test_tls_clue_extraction_value_fields`、`test_registry_and_direct_calls` |
| 覆盖度判据「族:条数」（防枚举任务一次请求交差；key 去重 + 任务作用域） | ✅ | `test_coverage_criteria_counts_distinct_keys` |
| 投影纪律：append 幂等命中返回 None，apply_event 只对真写入事件（修复 live/replay 分叉 + 同指纹任务 needs_help 空转） | ✅ | `test_die_at_step_and_resume_matches_uninterrupted`、`test_writer_resume_seq_and_actions` |
| 收口跑空队列：验收达成但队列未排空继续跑，排空后才 completed | ✅ | `test_completed_waits_for_queue_drain`、`test_completed_drains_queue_first` |

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
