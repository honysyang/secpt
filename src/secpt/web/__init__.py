"""secpt Web 控制台：浏览器版工作台（零新增依赖，stdlib HTTP + 轮询事件流）。

设计（2026-10-19）：
- 左侧导航：工作台（对话 + 执行轨迹 + 作战面板）/ 资产 / 发现 / 配置 / 工具 / 经验
- 数据源全部复用既有单点事实源：
    资产   → IntelStore（intel.sqlite，图记忆）
    发现   → FindingStore（findings.sqlite，confirmed/inferred 分级）
    工具   → CORE_TOOLS + tools/plugins（YAML/Python 插件）+ knowledge/techniques.yaml
    经验   → ExperienceJournal（experiences.jsonl + 使用统计）
    轨迹   → var/jobs/<id>/events.jsonl（seq 游标增量轮询；frontier 子任务已聚合进父流）
    作战面板 → frontier.sqlite（任务池）+ hypothesis.sqlite（假设树）+ strategy.md
- 任务提交：POST /api/jobs（objective + 附件 base64）→ 后台线程跑 dag.run_task
  （fake 模式传 FakeLLM，不污染进程环境变量；真实模式走 _prod_llm）。
- 配置：var/web/config.json（运行时覆盖项，新任务启动时应用到 os.environ）。

安全边界：默认只绑 127.0.0.1；静态文件服务做路径穿越防护；上传文件名净化。
"""

from __future__ import annotations
