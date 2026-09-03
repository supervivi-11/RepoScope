# 证据传递修复：离线验证记录

日期：2026-09-03。基线：PR #6 的 `f1c74c4`；分支：`codex/evidence-propagation`。

## 结论边界

本轮修复三个由代码和失败测试直接证实的信息丢失问题。没有调用真实模型，
没有重跑 development 或 hidden 评测，也没有修改上一轮结果或填写新分数。
旧评测只保留了最终预测与调用用量，没有保存逐节点报告/批判输出；因此无法
从现有产物断言这三个问题就是六个真实案例失败的全部原因。真实效果仍待重测。

## 已确认的问题与修复

| 问题 | 原先的确定性行为 | 本轮修复 |
| --- | --- | --- |
| 工具导航信息丢失 | `tool_dispatch._result_summary` 只返回类型和引用数；模块列表、相关 Issue 标题等无法进入模型上下文 | 传递有界 JSON 观察值，保留模块、路径、查询、命中类型和历史信息；源码仍在 citations 中，不重复拷贝 |
| 补查不携带缺口 | `_investigation_context` 不含上一轮 hypotheses/uncertainties，也不保留原始 Issue | 后续选工具与批判节点获得原始 Issue、假设和未决问题；注明搜索是字面子串而非布尔查询 |
| 已读证据需要模型重复誊写 | 假设已引用正确 commit/path/行号，但顶层 evidence 缺失时，验证器降级整个报告 | 只为模型明确选择的假设引用绑定已成功读过、重新校验有效的工具片段；初稿和一次修订共用相同逻辑 |

没有把全部源码自动挂到主假设，也没有直接接受模型宣称充分。
模型仍必须提出假设并明确选择支持它的引用；没有引用仍然证据不足。

## 安全与兼容边界

- 工具观察值标记为不可信数据；列表/字符串截断，整个观察值不超过 32 KiB，并明确标记截断。
- 模型上下文可读取观察值；公开工具事件仍只含原有安全字段，不新增源码、查询或历史正文日志。
- 片段补齐只能来自成功的工具调用记录，不使用 critique 自行生成的引用作为可信来源。
- 补齐前再次检查 commit、文件、行号和片段逐字一致性；之后仍执行原有最终报告验证器。
- 显式提供的错误片段不会被正确片段覆盖；未知路径、错误 commit、未读引用仍被拒绝。
- 保持报告数量和序列化字节上限，不改前后端报告契约，不增加工具、重试或调查预算。
- 新增 `report_evidence_bound` / `report_downgraded` 安全事件，区分引用补齐与验证降级；不含隐藏思维链。
- 未读取隐藏答案、修复 PR/diff 或修复后源码；没有付费请求。

## 测试记录

先补回归测试：原实现出现 8 项预期失败，包括导航结果不可解析、补查上下文缺失和已读引用被降级。
随后集中实现并补充安全、修订和截断用例。目录测试最初把模块名误认为文件路径，
已按现有领域契约将断言修正为 `src.parser`，没有改变模块映射。

- 定向测试：41 passed。
- 完整后端：392 passed，1 skipped（本机符号链接能力分支）。
- 前端：65 passed；TypeScript、live/static build 和构建模式隔离通过。
- Chromium：live 1 passed，static 3 passed，包含静态 Demo 零 API 请求断言。
- 前端限制生成器和评测 Schema 无漂移；12-case 公共数据集摘要验证通过。
- 发布扫描：Git 历史、暂存区、跟踪文件及 frontend/dist 均通过。
- 遵循用户本轮要求，不重复启动 reviewer；上述结果不冒称独立审查结论。

复现命令（仓库根目录）：

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests -q
.\.venv\Scripts\python.exe backend/scripts/generate_frontend_limits.py --check
.\.venv\Scripts\python.exe -m app.evaluation.cli validate-dataset --candidates evals/curation-candidates.v1.jsonl --cases evals/benchmark-cases.v1.jsonl --slots evals/benchmark-slots.v2.jsonl --development-gold evals/development-gold.v1.jsonl
npm --prefix frontend test -- --run
npm --prefix frontend run test:build-config
npm --prefix frontend run test:e2e
.\.venv\Scripts\python.exe backend/scripts/scan_release_secrets.py --repo-root . --artifact frontend/dist --allowlist security/secret-scan-allowlist.json
```

## 下一阶段

具备准备下一轮 development 重跑的离线条件，不代表真实效果已经达标。
下一轮使用新输出目录与固定代码 commit，保留修复前的结果，不覆盖 v1 产物。
确认付费预算后先检查报告引用与安全事件，再固定完整 6-case 成对运行；诊断运行与正式评分不得混为一谈。
未满足 development 门槛前不运行 hidden 集、不宣称效果提升。
