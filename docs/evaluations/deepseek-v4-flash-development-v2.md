# DeepSeek V4 Flash development 评测（v2）

## 结论

2026-09-03，锁定的六个 development 案例完成了一次完整成对运行和独立离线评分。
RepoScope 的 FileRecall@5 / MRR 均为 `0.166667`，低于本轮 Issue-only 的 `0.500000`。
只有 dateutil 案例产生了命中文件与两条有效引用，其余五例为 `insufficient_evidence`。
这不是产品效果达标：不进入 hidden 集，不自动追加付费重跑，不宣称优于基线。

PR #6、#7 已依次合并。重跑前封闭了评测访问可变 GitHub Issue 历史的入口，
详见 [前检查记录](development-rerun-v2-preflight.md)。生产历史工具不受该限制影响。

## 运行身份与边界

- Run ID：`20260903T031631Z-deepseek-v4-flash`。
- Clean source commit：`3ee8300aa7e708e470463039bce430447e3d5dc4`。
- UTC 起止：`2026-09-03T02:55:40.808151Z` 至 `2026-09-03T03:16:31.327391Z`，约 20 分 51 秒。
- 数据集 SHA-256：`de76c423ffe936743f979f35cc634caed064e571b3f5c9563b219ac5025ceea7`。
- 配置 SHA-256：`6711216c4cb8c803732356ea97dd8dc9d7d7cc90379fec896695914038b6c7f3`。
- DeepSeek 官方 Responses API；请求与所有可核验响应均为 `deepseek-v4-flash`；`provider_backend_drift=false`。
- Python `3.14.6`，不是 Python 3.12 实测；其余依赖版本在 manifest 中。
- thinking enabled / reasoning effort low / temperature 省略；每次输出最多 16,384 Token，总预算 2,500,000 Token；12 次工具、2 轮补查、2 次 Schema 重试。
- 只读取冻结 Issue 和修复前快照；评测历史门面拒绝 related issues / recent commits，不请求实时历史，拒绝仍占工具预算。
- 两组预测和哈希清单全部落盘、预测进程退出后，独立 scorer 才读取 development gold。没有读取 hidden gold、执行目标仓库代码或调用额外模型评分。
- `runner_version` / `prompt_version` 配置标签沿用 v1；它们不代表执行代码与模型上下文未变化，必须同时比较 source commit 和输入策略。

## 真实聚合结果

| 指标 | Issue-only | RepoScope |
| --- | ---: | ---: |
| FileRecall@5 | 0.500000 | 0.166667 |
| MRR | 0.500000 | 0.166667 |
| 引用数量 | 0 | 2 |
| 引用有效率 | null | 1.000000 |
| 幻觉引用率 | null | 0.000000 |
| 中位延迟 | 19.247 s | 163.720 s |
| 输入 Token | 3,180 | 1,265,507 |
| 输出 Token | 15,650 | 147,845 |
| 估算成本 | $0.020857584 | $0.689707504 |

RepoScope 相对本轮基线的 FileRecall@5 与 MRR 差值均为 `-0.333333`。
预检另外消耗 3,243 输入、1,447 输出 Token，估算 $0.002082816。
整轮合计 **1,436,872 Token，估算 $0.712647904**；共 90 次模型尝试（含 5 次预检和 3 次失败尝试）。
3 次失败均在 Click 的工具选择或证据审查阶段发生，错误为 `schema_error`，均在既定次数内恢复。
本次没有重启整轮。费用依据冻结费率卡 `deepseek-v4-2026-08-16-v1`，实际账单以供应商平台为准。

引用有效率只验证 commit、路径、行号和 excerpt，不证明根因正确或证据语义充分。
两条引用都来自一个案例，不能把这个小样本的 100% 宣传为普遍可靠性。
Issue-only 的 null 表示未发出引用，而非 100% 有效。`successful_cases=6` 只表示管线成功返回结果，不表示六例找到根因。

## 逐案例结果

| Case | Issue-only 首次命中排名 | RepoScope 首次命中排名 | RepoScope 结果 | 工具数 |
| --- | ---: | ---: | --- | ---: |
| dateutil-dateutil-issue-926 | 1 | 1 | `dateutil/tz/tz.py`，2 条有效引用 | 12 |
| hynek-structlog-issue-476 | 未命中 | 未命中 | 证据不足 | 3 |
| pallets-click-issue-2819 | 未命中 | 未命中 | 证据不足 | 12 |
| pallets-flask-issue-2267 | 1 | 未命中 | 证据不足 | 6 |
| pyinvoke-invoke-issue-533 | 1 | 未命中 | 证据不足 | 8 |
| tox-dev-platformdirs-issue-207 | 未命中 | 未命中 | 证据不足 | 12 |

## 与 v1 的比较和限制

[v1 原始结果](deepseek-v4-flash-development-v1.md) 保持不变。RepoScope 文件召回从 0 变为 0.166667，
空引用从六例减少到五例；与此同时 Issue-only 从 0.666667 变为 0.500000。
同一配置的单次生成仍会变化，不能将两轮差异视作统计显著的效果提升。
本轮同时改变了证据传递和历史工具输入策略，不能把差异全部归因于证据传递修复。

现有机器产物保留最终文件、引用、报告 outcome 和调用用量，但不导出逐工具事件、
中间假设、引用拒绝码或完整报告。因此这五例为何降级还不能从公开产物逐一确定，
也不能仅凭 `root_cause_identified` 标签声称 dateutil 根因已被人工验证。
下一开发里程碑应先补齐无隐藏思维链、无答案泄漏的诊断导出及离线回放测试，
再根据观察证据修复具体丢失环节；不应仅因本轮未达标就提高预算或换模型。
若需要再次调用真实模型，应单独确认小规模诊断范围与预算，诊断不充当正式 benchmark。

Hidden 门槛：FileRecall@5 至少 0.70、比 Issue-only 高至少 0.20、引用有效率 100%、幻觉引用率 0%。
本轮前两项未通过；中位延迟虽低于 3 分钟，也不能替代效果门槛。Hidden 保持未运行。
公网三个 Demo 仍是明确标注的产品流程占位数据，不替换成未经核验的“真实成功案例”。

## 验证与复现

本轮历史隔离修复：定向测试 8 passed；完整后端 394 passed / 1 skipped；独立聚焦审查无 Critical / Important 问题。
离线 scorer 成功验证配置、数据集、源码快照、两组预测、账本重试一致性与 manifest 哈希，再计算六例成绩。
前端未改动，不将之前的前端测试冒称本轮重新执行结果。

产物在 `evals/runs/deepseek-v4-flash-development-v2/`，包含配置、两组 result JSONL、call ledger、manifest、summary 与 `reproduce.txt`。
只重新评分（不调用模型）：

```powershell
.\.venv\Scripts\python.exe -m app.evaluation.development_score --cases evals/benchmark-cases.v1.jsonl --dataset-digest-file evals/benchmark-cases.v1.sha256 --snapshots-root local/evaluation/snapshots --prediction-directory evals/runs/deepseek-v4-flash-development-v2 --development-gold evals/development-gold.v1.jsonl --output local/evaluation/development-v2-reproduced-summary.json
```

需要本地修复前快照。真实复跑须使用自行提供的 Key，并将 `reproduce.txt` 两条命令的目录一起改为新的独立目录；不得覆盖已发布 v1/v2 产物。
