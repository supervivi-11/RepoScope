# Development v2 重跑前检查

2026-09-03：用户批准按顺序合并 PR #6、#7 并进入 development 重测。
两项已合并至 main，合并提交分别为 `7d057bc`、`7c119a9`。

## 付费前发现并封闭的输入风险

评测原先将实时 GithubClient 注入调查工具。新增工具观察值会保留当前 Issue 正文，
而 related-issue 查询不能还原历史正文；源码 SHA 与本地 gold 文件隔离不保护这个入口。
审查确认该通路可达，但没有证据证明旧 v1 实际读取过修复后信息，不能据此宣称已泄漏。

本轮仅在 RealRepoScopeAnalyzer 中使用不保存、不委派实时客户端的离线历史门面。
get_related_issues / get_recent_commits 均在发请求前拒绝，dispatcher 将其记为安全失败，
不伪装成成功查询且空结果。被拒调用仍占 12 次工具预算。生产历史工具保持原行为。
原 recent commits 已锚定修复前 SHA；同时关闭它是保守的离线输入策略改变，
不是已证实存在同样的时间泄漏。因此 v1/v2 不能解读为只改变证据传递的单变量实验。

回归用假模型、合成源码和带 POST_FIX_CANARY 的客户端，检查两种历史调用均为零、
无历史正文进入上下文、工具明确失败，而随后 read_code 与最终引用校验仍正常。

## 本次运行边界

- 仍使用固定官方 deepseek-v4-flash / thinking enabled / low 配置。
- 每次输出最多 16384 Token；总上限 2,500,000 Token；12 工具、2 证据轮次、2 次已核算 Schema 重试。
- 只运行锁定的 6 个 development 案例与修复前源码，先完成两组预测再离线读取 development gold 评分。
- 新目录 evals/runs/deepseek-v4-flash-development-v2，保留 v1 原始产物不变。
- Key 仅从正式用户环境临时注入进程，永不打印、落盘或提交。
- 整体运行失败后停止，不自动启动第二次实验，不提高预算，不进入 hidden 集。
- 配置沿用原版本；代码与输入策略差异由新的 clean source commit 和本记录披露。

这是重跑前记录，不是结果报告；没有在这里填写任何新 benchmark 成绩。
