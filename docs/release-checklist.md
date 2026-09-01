# RepoScope v0.1.0 发布检查清单

本文件是发布候选清单，不表示项目已经发布。Task 7 只准备材料；Task 8 执行并记录观察结果。

## 代码与契约

- [x] 分支从预期 Task 7 commit `a8cc704` 继续，Task 8 提交后工作树干净
- [x] 后端全量测试在 warnings-as-errors 下通过（276 passed，1 skipped）
- [x] 前端 Vitest、typecheck、静态/实时构建通过
- [x] Playwright Chromium 场景实际执行，不只是 `--list`（live 1，static 3）
- [x] 数据库从空库迁移到 head（`20260830_0001`）
- [x] Docker Compose 从空环境启动并通过健康检查
- [x] 静态 Demo 构建不发出 API/model 请求
- [x] 报告/前端限制生成器与评测 Schema `--check` 通过

## 安全

- [x] secret scan 覆盖 Git 历史、当前跟踪文件与构建产物
- [x] `.env`、密钥、数据库、快照、hidden gold 与本地结果未跟踪
- [x] GitHub allowlist、ZIP Slip、符号链接、大小限制与日志脱敏回归通过
- [x] 公网构建没有 live API 配置或 provider 凭据
- [x] 快照 janitor 与评测临时目录清理经过验证
- [ ] SECURITY 中的披露渠道可用

## 评测真实性

- [ ] 十二个 case 均通过人工选择条件（当前 6 个合格 pre-split candidates，尚余 6 个）
- [x] 6/6 metadata-only 槽位保持 `unfilled`；选满 12 个前不提前分组
- [x] pre-split 修复证据与 gold 仅保存在被忽略的 `local/evaluation/`，未提交、未传给 runner
- [ ] 真实结果保留 case digest、模型与 rate-card 版本
- [x] README 中的所有数字来自可复现结果或明确写为目标/未执行
- [x] 未运行真实评测，所有页面继续明确写“未执行/无成绩”

## 作品集

- [x] 中文 README 与英文摘要准确
- [x] 架构图与安全边界和代码一致
- [x] 三个 Demo 仍是占位，并继续显著标注为产品流程演练
- [ ] 60 秒 GIF 与 3–5 分钟视频无密钥、本地路径或误导性分数
- [ ] 面试讲解能在五分钟内完成
- [x] CONTRIBUTING、SECURITY、MIT License 完整

## GitHub 发布（需用户明确执行）

- [x] 独立 reviewer 未发现 Critical 或 Important 问题
- [x] `docs/release-verification.md` 记录 Task 8 的命令与真实输出
- [ ] Conventional Commit 历史清晰
- [x] 准备 `v0.1.0` Release 标题、变更摘要、限制与校验信息
- [x] Task 8 未 push、部署、打 tag 或发布；后续必须由用户明确确认
