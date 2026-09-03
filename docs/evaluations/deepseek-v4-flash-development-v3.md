# DeepSeek V4 Flash development 评测（v3）

## 结论

2026-09-03，锁定的六个 development 案例在引用重锚定修复（`7485ad4`）后完成了一次完整成对运行和独立离线评分。
RepoScope 的 FileRecall@5 / MRR 均为 `0.666667`，高于本轮 Issue-only 的 `0.500000`（差值 `+0.166667` / `+0.291667`）。
六例中四例产出 `root_cause_identified` 且首个命中文件均排在第 1 位；共发出 20 条引用，20 条通过校验（有效率 100%，幻觉率 0%）。
v2 的五例 `insufficient_evidence` 收窄为两例（flask、invoke）。

这是开发集上的达标运行（超过本轮 Issue-only 基线），但**未过 hidden 门槛**（FileRecall@5 需 ≥0.70 且领先基线 ≥0.20，本轮为 0.667 / +0.167）：
不进入 hidden 集，不宣称产品效果达标。单次生成存在波动（见"与 v2 的比较"），两轮差异不能视作统计显著。

## 运行身份与边界

- Run ID：`20260903T095547Z-deepseek-v4-flash`。
- Clean source commit：`30eb3c8456fab7264c59a9b8e2370af85d224dbf`，`working_tree_clean=true`。
- UTC 起止：`2026-09-03T09:35:23.902876Z` 至 `2026-09-03T09:55:47.578045Z`，约 20 分 24 秒。
- 数据集 SHA-256：`de76c423ffe936743f979f35cc634caed064e571b3f5c9563b219ac5025ceea7`（与 v1/v2 相同，锁定数据集未动）。
- 配置 SHA-256：`6711216c4cb8c803732356ea97dd8dc9d7d7cc90379fec896695914038b6c7f3`（与 v2 相同，冻结运行配置未动）。
- DeepSeek 官方 Responses API；请求与所有可核验响应均为 `deepseek-v4-flash`；`provider_backend_drift=false`。
- Python `3.14.6`；依赖版本在 manifest（httpx 0.28.1、langchain-openai 1.6.0、langgraph 1.2.11、openai 3.6.0、pydantic 2.13.5、pydantic-settings 2.15.0）。
- thinking enabled / reasoning effort low / temperature 省略；每次输出最多 16,384 Token，总预算 2,500,000 Token；12 次工具、2 轮补查、2 次 Schema 重试。
- **注意**：`prompt_version` 标签沿用 `reposcope-eval-v1`，但本轮修改了调查/报告指令文本与上下文组装（去重、充分性标准、重锚定指引）；比较执行差异必须以 source commit 为准，不能以标签为准。
- 只读取冻结 Issue 和修复前快照；评测历史门面拒绝 related issues / recent commits。两组预测和哈希清单全部落盘、预测进程退出后，独立 scorer 才读取 development gold。没有读取 hidden gold、执行目标仓库代码或调用额外模型评分。
- 本轮起正式运行同步落盘逐节点诊断 `diagnostics.v1.jsonl`（无隐藏思维链、无答案泄漏、封闭 schema），v2 "无法从公开产物确定降级原因"的限制已消除。

## 真实聚合结果

| 指标 | Issue-only | RepoScope |
| --- | ---: | ---: |
| FileRecall@5 | 0.500000 | 0.666667 |
| MRR | 0.375000 | 0.666667 |
| 引用数量 | 0 | 20 |
| 引用有效率 | null | 1.000000 |
| 幻觉引用率 | null | 0.000000 |
| 中位延迟 | 22.445 s | 137.364 s |
| 输入 Token | 4,464 | 809,630 |
| 输出 Token | 21,563 | 147,900 |
| 估算成本 | $0.028736952 | $0.491811568 |

预检另外消耗 3,265 输入、1,060 输出 Token，估算 $0.001581656。
整轮合计 **987,882 Token，估算 $0.522130176**；共 91 次模型尝试（含 5 次预检和 7 次失败尝试）。
7 次失败均为 `schema_error`（structlog 工具选择×2 与报告、flask 工具选择、click 与 invoke 的 issue-only 预测、invoke 报告），均在既定重试次数内恢复，未重启整轮。
费用依据冻结费率卡 `deepseek-v4-2026-08-16-v1`，实际账单以供应商平台为准。

引用有效率只验证 commit、路径、行号和 excerpt，不证明根因正确或证据语义充分。
Issue-only 的 null 表示未发出引用，而非 100% 有效。`successful_cases=6` 只表示管线成功返回结果，不表示六例找到根因。

## 逐案例结果

| Case | Issue-only 首次命中排名 | RepoScope 首次命中排名 | RepoScope 结果 | 引用数 | 工具数 | 案例成本 |
| --- | ---: | ---: | --- | ---: | ---: | ---: |
| dateutil-dateutil-issue-926 | 未命中 | 1 | `dateutil/tz/tz.py`，root_cause_identified | 6 | 12 | $0.0880 |
| hynek-structlog-issue-476 | 未命中 | 1 | `src/structlog/_log_levels.py`，root_cause_identified | 1 | 3 | $0.0418 |
| pallets-click-issue-2819 | 1 | 1 | `src/click/core.py`，root_cause_identified | 7 | 12 | $0.1231 |
| pallets-flask-issue-2267 | 1 | 未命中 | 证据不足（primary_unvalidated） | 0 | 7 | $0.0757 |
| pyinvoke-invoke-issue-533 | 4 | 未命中 | 证据不足（primary_unvalidated） | 0 | 12 | $0.1022 |
| tox-dev-platformdirs-issue-207 | 未命中 | 1 | `src/platformdirs/unix.py`，root_cause_identified | 6 | 6 | $0.0609 |

两个未达标案例的诊断（来自本轮正式产物 `diagnostics.v1.jsonl`）：

- **flask-2267**：模型再次产出 `root_cause_identified`；6 条引用中 3 条通过快照校验（重锚定修复生效的部分），
  另外 3 条 `excerpt_mismatch` 被拒；但主假设的引用指向被拒位置，`_filter_hypothesis` 清空主证据 → 整报降级。
  与修复前"全部引用被拒"相比通道收窄，但主假设引用了证据池外的合成位置时仍会被清空。
  同案例两次单例诊断运行（修复后）均 `supported_primary` 且命中，正式运行未命中——同一配置的单次生成会波动。
- **invoke-533**：12 次工具调用中 7 次 `read_code` 失败（无效行范围），浪费预算并限制证据池（34 条）；
  报告主假设引用与证据池位置零匹配（`hypothesis_tool_matches=0`），无顶层引用可校验（valid_count=0，无拒绝码）→ 降级。
  该案例同时暴露 A 层（导航摩擦）与 C 层残余（引用位置漂移出池）。

## 与 v2 的比较和限制

[v2 结果](deepseek-v4-flash-development-v2.md)与 [v1 结果](deepseek-v4-flash-development-v1.md)保持不变，已发布产物未被覆盖。

| 指标（RepoScope） | v1 | v2 | v3 |
| --- | ---: | ---: | ---: |
| FileRecall@5 | 0.000000 | 0.166667 | 0.666667 |
| insufficient_evidence 案例 | 6/6 | 5/6 | 2/6 |
| 引用（发出/有效） | 0/0 | 2/2 | 20/20 |
| 输入 Token | — | 1,265,507 | 809,630 |
| 估算成本 | — | $0.689707504 | $0.491811568 |
| 中位延迟 | — | 163.720 s | 137.364 s |

v2→v3 的代码变化：诊断导出（PR 9）、`--case` 部分诊断模式、引用重锚定修复（`bind_tool_evidence` 对已验证工具位置的漂移 excerpt 以快照原文替换）、上下文去重（tool_history 不再重复携带全文引用）、指令校准（充分性标准、检索重试策略、精确复制位置指引）。
翻转模式（四例报告存活且引用全部有效、输入 Token 下降 36%）与重锚定修复和去重的预期一致，且 flask 案例有修复前后单例诊断的对照证据（`primary_unvalidated` → `supported_primary`）。
但本轮同时包含多项变化，且 Issue-only 基线自身也在两轮间波动（dateutil 从命中变为未命中、invoke 从排名 1 变为 4），不能把全部差异归因于单项修复，也不能将单次运行差异视作统计显著。

残余限制：
- C 层通道未根除——主假设引用池外合成位置时仍触发整报降级（flask）；下一步候选是"位置级重锚定"（对快照内有效但未读取过的范围以原文替换），需单独设计评审其安全取舍。
- A 层（检索/导航摩擦）未修：invoke 的 7 次 read_code 失败表明错误信息不足以引导模型修正行范围；阶段 2 探针发现的检索缺口留档。
- B 层校准后 structlog 从 3 次工具即放弃变为 3 次工具即命中，但样本仅一例，不能推广。
- 4/6 案例发出引用仍是小样本，不能把 100% 有效率宣传为普遍可靠性。

Hidden 门槛：FileRecall@5 至少 0.70、比 Issue-only 高至少 0.20、引用有效率 100%、幻觉引用率 0%。
本轮后两项通过、前两项未通过（0.667 / +0.167）。Hidden 保持未运行。
公网三个 Demo 仍是明确标注的产品流程占位数据，不替换成未经核验的"真实成功案例"。

## 验证与复现

本轮修复的测试基线：完整后端 **432 passed / 1 skipped**（`-W error`）；schema 导出无漂移；评测 CLI 离线评分通过（本文档数字来源）。
离线 scorer 成功验证配置、数据集、源码快照、两组预测、账本重试一致性与 manifest 哈希，再计算六例成绩；评分进程未调用模型。
前端未改动，不将之前的前端测试冒称本轮重新执行结果。

产物在 `evals/runs/deepseek-v4-flash-development-v3/`，包含配置、两组 result JSONL、call ledger、逐节点诊断、manifest 与 `reproduce.txt`。
只重新评分（不调用模型）：

```powershell
.\.venv\Scripts\python.exe -m app.evaluation.development_score --cases evals/benchmark-cases.v1.jsonl --dataset-digest-file evals/benchmark-cases.v1.sha256 --snapshots-root local/evaluation/snapshots --prediction-directory evals/runs/deepseek-v4-flash-development-v3 --development-gold evals/development-gold.v1.jsonl --output local/evaluation/development-v3-reproduced-summary.json
```

需要本地修复前快照。真实复跑须使用自行提供的 Key，并将 `reproduce.txt` 两条命令的目录一起改为新的独立目录；不得覆盖已发布 v1/v2/v3 产物。
修复过程的完整排查记录见 `docs/development-log.md`（阶段 2 离线定位 → 阶段 3 单例诊断实证 → 阶段 4 修复与验证）。
