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

## 2026-10-15 产品追加审查：本地修正，验收仍未通过

已修复前置阻断覆盖具体澄清，以及未确认方法却采用方法限定数值卡；Agent package exact/semantic 最终门禁同步隔离。补齐明确方法控制和未确认方法澄清回放，不改 SDK 样本答案。Agent 716 项通过；KB 相关 187 项通过，fixture 测试 18 项通过；产品路由 31/31、任务契约 16/16。完整 CI 149 项仍有 2 个发布审计失败；旧治理测试 3 项失败；3 个 WSL validator 依赖测试待验，不能自动豁免。

原 10 组 SDK 输入确与前一候选固定提交一致；此次不重跑付费模型，第 6 组改为零模型澄清。未宣称生产错误或业务验收成功。方法匹配仍为词面家族，否定/不确定语义及跨轮已确认方法需后续审查。

导航/附件/上下文先交诊断和范围选项，未实施权限或会话架构变化：hybrid 丢弃 SOP 导航结果；SOP501 有受管导航/原件元数据而非参数通行权；生育工作簿实际含 NIFTY 所需配置，静态工具及平台具有文件读取/生成基础和发送能力，原 SDK 能力否认不成立。legacy 缺 typed confirmed context 提供者，禁止从全历史猜产品。

完整报告与证据位于 /Users/slim/IVD-maintenance-reports/2026-10-14/expression-product-review-followup-20261015.md。状态仍为本地 candidate_only、未发布。

## 2026-10-15 收敛范围补充（替代前次宽泛绑定）

本轮关闭方法否定/疑问/不确定/冲突误确认，并修复本次来源附加导致的单值 hybrid 路由退化；来源编号按完整绑定 ID 匹配。Agent 受影响 159 项、KB 111 项通过，31 句回放两端一致（21 澄清、9 手工数值、1 自动方法不套手工卡且未调用模型）。独立规格、质量审查 APPROVED，仅覆盖代码。

两项发布审计旧失败归属 Darwin FD 路径兼容；三项治理旧失败归属流程/上下文、澄清表示、产品分类契约；基线同样复现，未修改旧模块或断言。导航、xlsx、typed context 独立 backlog，不再全部绑定本轮实现。仍为本地 candidate_only、未发布；WSL/R6、真实客户端、业务及自然度验收未通过。

完整收敛证据：/Users/slim/IVD-maintenance-reports/2026-10-14/expression-convergence-summary-20261015.md。

## 2026-10-15 待确认状态定向修复

方法待确认/待确定接入既有不确定与明确重置优先关系：后置待确认澄清，之后明确确认可恢复。KB numeric 11项、Agent engine/hybrid 106项通过；36句回放含原31句控制。独立spec/quality代码复核APPROVED。数值正文仍依赖问句承载方法与对象，出处仍为受控摘录行，不冒充原件章节。本地candidate_only未发布。证据：/Users/slim/IVD-maintenance-reports/2026-10-14/expression-pending-method-summary-20261015.md。
