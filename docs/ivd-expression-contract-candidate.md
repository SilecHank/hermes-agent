# 回复表达升级：本地候选交接（2026-10-15）

状态：candidate_only，代码审查通过，业务效果验收待完成，未发布。

统一展示契约为 scripts/hermes_channel_presentation.py 的 hermes-expression-v1；旧 after_sales_platform_policy 为兼容适配器。模型生成入口消费同一契约，答案卡保留完整原文并附加已核验公开来源。缺失或旧展示接口保留旧行为。展示变化继续经过原有事实及最终校验，不增加润色模型调用。

Agent 同步修改普通/专家提示入口、package 直出及澄清、legacy 答案卡，以及微信/企业微信发送。微信保护数字单位和 SOP/版本，企业微信分段保留完整正文。实际网络发送尚未验收。

本地检查：Agent 32 文件 710 项通过；知识库相关回归 137 项通过；产品路由回放 30/30，任务契约回放 15/15。3 项 WSL 活跃数值校验器依赖测试在 Mac 跳过。知识库完整 CI 有 2 项失败、1 项跳过，两项失败在未修改基线同样复现；旧治理测试另有 3 项既存失败。不得称全量 CI 通过，发布前仍需 Linux/WSL 门禁。

10 组真实模型 API 前后对照已完成（5 个历史问题、5 个构造问题；历史原回复未恢复）。候选 6/10 更短，总字数 4309→4084，机械筛查未发现机器字段或 Markdown。第 2 组未核实数值和第 6 组血液分离/提取范围匹配仍需审查；源摘录不等于逐条事实授权。SDK 对照未穿过完整生产工具及最终门禁。没有人工五项评分、真实成员反馈或生产效果指标，表达/事实验收未通过。

独立遗留问题：legacy planner 对话产品/样本上下文丢失；PMseq RNA V5 SOP 导航的 capability/evidence 契约阻断。仅澄清措辞已改善，功能问题未修复。

证据目录：/Users/slim/IVD-maintenance-reports/2026-10-14。主报告 expression-upgrade-candidate-summary.md；逐题样本 expression-model-comparison.md；上下文排查 context-reachability.md。

两个本地候选 worktree 分支均为 feat/expression-contract-20261014。没有 push、PR、merge、部署、正式知识晋升、阈值改动、主备切换或定时监听。后续产品审查通过后另行申请发布，走双仓库固定提交、完整 R6 发布门禁及既有回滚路径。48–72 小时仅为未来真实观察窗口，未创建自动任务。

独立审查：expression_final_spec APPROVED；expression_final_quality 在修复旧展示 API 来源附加兼容问题后 APPROVED。审查结论覆盖代码，不等同业务效果验收。
