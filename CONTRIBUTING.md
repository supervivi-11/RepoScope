# Contributing to RepoScope

## 开始前

先阅读 `README.md`、`docs/architecture.md`、`SECURITY.md` 和当前实施计划。新需求必须先判断是否突破 v1 边界；Java、私有仓库、代码执行、自动改代码、多 Agent、账号计费和公网模型服务不属于 v1。

## 分支与提交

- 从 `main` 创建 `codex/<topic>` 或含义清晰的功能分支；
- 使用 Conventional Commits，例如 `feat: ...`、`fix: ...`、`docs: ...`；
- 每个 PR 聚焦一个能力，写明用户价值、验收条件、测试命令与安全影响；
- 不提交 `.env`、API Key、数据库、下载快照、hidden gold 或真实本地评测结果。

## 开发流程

1. 先写能够因缺失行为而失败的测试；
2. 运行测试并确认失败原因；
3. 写最小实现，再运行聚焦测试；
4. 阅读 diff，检查只读、安全、恢复、异常和兼容边界；
5. 在 PR 前运行后端、前端、类型检查与构建；
6. 重要变更必须由独立 reviewer 检查 Critical/Important 问题。

```powershell
.\.venv\Scripts\python.exe -m pytest -W error -q backend\tests
Set-Location frontend
npm test -- --run
npm run typecheck
npm run build
```

## 评测贡献

数据策展必须遵守 `evals/README.md`。Case 与 gold 物理分离；hidden gold 不公开。PR 不得声称目标分数已达成，除非附带可复现命令、原始版本化结果、模型/rate-card 标识和完整运行限制。Scripted 输出、Demo 占位数据与手工示例都不是 benchmark 成绩。
