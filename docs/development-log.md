# 开发过程日志（recall-repair-v1）

本文件是 `codex/recall-repair-v1` 开发周期的关键步骤与排查记录，append-only。只记关键步骤以节省篇幅：

- **阶段条目**：每阶段收尾 1 条（≤10 行）：阶段、日期、commit、关键命令与结果摘要、产物位置。
- **排查条目**：仅当出现明显运行时错误/指标异常时追加，固定模板：现象 → 排查路径 → 根因 → 修复 → 验证 → 遗留风险。
- **付费运行条目**：run id、案例范围、Token/成本、指标变化，细节链接正式评测文档，不复制内容。
- 不记常规成功操作与可从 git log / 评测文档直接读到的内容；推测须标注"推测"。

---

## 2026-09-03 基线条目（阶段 0）

- 分支：`codex/recall-repair-v1`，基于 `36cbf54`（= origin/main `ac8bd8b` + PR8/9 合并验证文档）。fetch 确认远端无新提交。
- 工作位置：`.worktrees/reposcope-v1`（主工作区留在 `92f9251`，收尾时同步）。
- 环境就位：`.venv` Python 3.14.6；`frontend/node_modules` 已存在；Node v24.9.0；Docker 29.4.2 可用。
- 数据就位：`local/evaluation/snapshots` 含全部 12 个冻结快照；`evals/development-gold.v1.jsonl` 在库内；hidden gold 仅在 `local/`（本轮不触碰）。
- 问题基线：真实 DeepSeek development 评测 v1 RepoScope FileRecall@5 = 0.0（0 引用）；v2 = 0.1667，5/6 案例 `insufficient_evidence`，低于同轮 issue-only 基线 0.5。PR 9 已加入逐节点安全诊断，但尚无真实运行诊断数据。
- 本轮目标：程序端到端正常运行 + 新一轮 development 运行 FileRecall@5 > 0、≥4/6 案例有效引用、引用有效率 100%；争取 ≥ issue-only 基线。
- 本轮约定：不做逐次 review，测试为门槛，运行时报错才深入审计；付费模型运行前逐次向用户确认预算；不覆盖已发布 v1/v2 产物。
