# RepoScope v0.1.0 发布检查清单

本文件是发布候选清单，不表示项目已经发布。Task 7 只准备材料；Task 8 执行并记录观察结果。

## 代码与契约

- [ ] 分支基于预期 commit，工作树干净
- [ ] 后端全量测试在 warnings-as-errors 下通过
- [ ] 前端 Vitest、typecheck、静态/实时构建通过
- [ ] Playwright Chromium 场景实际执行，不只是 `--list`
- [ ] 数据库从空库迁移到 head
- [ ] Docker Compose 从空环境启动并通过健康检查
- [ ] 静态 Demo 构建不发出 API/model 请求
- [ ] 报告/前端限制生成器与评测 Schema `--check` 通过

## 安全

- [ ] secret scan 覆盖 Git 历史与构建产物
- [ ] `.env`、密钥、数据库、快照、gold 与本地结果未跟踪
- [ ] GitHub allowlist、ZIP Slip、符号链接、大小限制与日志脱敏回归通过
- [ ] 公网构建没有 live API 配置或 provider 凭据
- [ ] 快照 janitor 与评测临时目录清理经过验证
- [ ] SECURITY 中的披露渠道可用

## 评测真实性

- [ ] 十二个 case 均通过人工选择条件
- [ ] 6/6 split 在模型运行前锁定
- [ ] hidden gold 未提交、未传给 runner
- [ ] 真实结果保留 case digest、模型与 rate-card 版本
- [ ] README 中的所有数字来自可复现结果
- [ ] 若未运行真实评测，所有页面继续明确写“未执行/无成绩”

## 作品集

- [ ] 中文 README 与英文摘要准确
- [ ] 架构图与安全边界和代码一致
- [ ] 三个 Demo 是真实历史导出；若仍是占位，继续显著标注
- [ ] 60 秒 GIF 与 3–5 分钟视频无密钥、本地路径或误导性分数
- [ ] 面试讲解能在五分钟内完成
- [ ] CONTRIBUTING、SECURITY、MIT License 完整

## GitHub 发布（需用户明确执行）

- [ ] PR review 不存在 Critical 或 Important 问题
- [ ] `docs/release-verification.md` 记录 Task 8 的命令与真实输出
- [ ] Conventional Commit 历史清晰
- [ ] 准备 `v0.1.0` Release 标题、变更摘要、限制与校验信息
- [ ] 用户确认后再 push、部署或发布；自动化代理不得擅自执行
