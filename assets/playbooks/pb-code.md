---
id: pb-code
applies_to: [code]
trigger: 附件/源码审计任务没有明确推进阶段时
summary: 源码审计五阶段：清单 → 依赖 → 入口面 → 危险 sink → 复核归档，逐阶段过门
---
# 源码审计方法论（playbook）

按阶段推进，每阶段以证据集合定进入下一阶段的资格；上一阶段证据不足就原地补。

1. 清单：fs.list 附件目录、fs.read 关键清单文件（README/配置/入口），建立「这是什么软件、什么栈」的基线。
2. 依赖：fs.read requirements.txt / package.json / go.mod，逐条核对引擎 findings 台账里的 advisory 提案——命中后用 grep 确认该依赖确实被 import/调用。
3. 入口面：grep 路由/接口/反序列化点（@app.route、http handler、pickle.loads、yaml.load），列出「不可信输入进来的所有位置」。
4. 危险 sink：按类 grep（eval/exec/os.system/shell=True/os.popen/sql 拼接/verify=False/md5·sha1），每个命中回读上下文 ±30 行，追踪参数来源是否可达入口。
5. 复核归档：每条候选漏洞按「入口 → 传播 → sink → 影响」四要素补齐证据；缺一环交 partial 并列缺口——不许为凑结论把可疑升级成可利用。

收尾判据自查：先看 findings 台账里自己 origin 的提案是否都有 ≥1 条可核验证据，再核对覆盖度（族:条数），最后才 finish。
