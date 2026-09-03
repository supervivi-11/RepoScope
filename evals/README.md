# RepoScope v1 评测契约

## 当前真实性状态

- `curation-candidates.v1.jsonl` 保存 12 个通过人工核验、尚未带答案的原始候选，作为锁定算法的审计输入。
- `benchmark-cases.v1.jsonl` 是两个 runner 唯一可读取的 12-case 数据集；不含修复 PR、diff、fix commit、changed paths 或 gold。
- `benchmark-slots.v2.jsonl` 保存确定性的 6/6 锁定映射；`benchmark-slots.v1.jsonl` 仅作为 Task 7 的空槽历史契约保留，不再表示当前状态。
- `development-gold.v1.jsonl` 公开 6 个开发集答案；hidden gold、修复证据和源码快照只存在于被 Git 忽略的 `local/evaluation/`。
- `benchmark-cases.v1.sha256` 固定完整数据集指纹：`de76c423ffe936743f979f35cc634caed064e571b3f5c9563b219ac5025ceea7`。
- DeepSeek Flash 于 2026-09-03 完成第二轮真实 development-only 6-case 运行：Issue-only FileRecall@5 为 `0.500000`，RepoScope 为 `0.166667`；2 条引用均有效，但五例仍证据不足。完整结果和成本见 `docs/evaluations/deepseek-v4-flash-development-v2.md`；v1 原始结果保留。Hidden 未运行。

## 数据与答案必须物理分离

在线评测现已增加封闭字段的逐节点 `diagnostics.v1.jsonl`，辅助定位证据丢失；
诊断不会进入模型上下文，旧 v1/v2 结果保持不变且无法事后补齐。实现与离线验证见
[安全诊断说明](../docs/evaluations/safe-diagnostics-v1.md)。本次开发没有新增真实评测成绩。

```text
benchmark-cases.v1.jsonl ──> Issue-only / RepoScope runner ──> result.v1 JSONL

development/hidden gold ────────────────────────────────────> scorer ──> summary.v1 JSON
```

Runner 只能接收 case。Gold 只在对应 split 的模型调用全部结束后交给 scorer，禁止先加载含答案的对象再靠约定忽略字段。发布安全扫描会拒绝公开 runner 文件中的修复元数据和 `gold_files`，也会拒绝任何位置被 Git 跟踪的 `hidden-gold` 文件。

### 版本化契约

- `reposcope.eval.candidate.v1`：Issue、pre-fix SHA 与快照 digest；无 split 和答案。
- `reposcope.eval.case.v1`：加入确定性 split 后的 runner 输入；仍无答案。
- `reposcope.eval.slot.v2`：slot、case 和 split-key digest 的锁定记录。
- `reposcope.eval.gold.v1`：case ID 与 1–5 个 pre-fix 快照中真实存在的 Python 生产文件。
- `reposcope.eval.result.v1`：系统、预测、引用、错误、dataset digest 与可空 usage。
- `reposcope.eval.summary.v1`：Task 7 的历史摘要契约，保留兼容。
- `reposcope.eval.summary.v2`：在指标和成对差值之外，强制保存 split 与完整 12-case dataset digest。
- `reposcope.eval.run-config.v1`：固定 DeepSeek 官方地址、Flash 模型、思考模式、Token/工具/重试预算与费率来源，不含 API Key。
- `reposcope.eval.call-usage.v1`：逐次记录模型、fingerprint、成功/失败、延迟、缓存输入、输出、思考 Token 和版本化成本。
- `reposcope.eval.manifest.v1`：覆盖配置、两组原始结果、usage 与复现命令的 SHA-256 清单。

JSON Schema 位于 `evals/schemas/`。JSONL 使用 UTF-8、键排序、紧凑分隔符、LF 与末尾换行；读取器拒绝 BOM、重复 key、NaN/Infinity、重复 ID、过大行和非规范路径。

## 十二个历史 Bug 的选择边界

每个 case 均满足：公开 Python 仓库；已关闭 Issue 和单一目的已合并修复；冻结 Issue 与修复前 commit；修复不超过 8 个文件；gold 为 pre-fix 中已存在的 1–5 个 Python 生产文件；排除测试、文档、生成物、vendor、重命名、新增/删除文件和宽泛重构；Issue 不直接泄漏答案；静态调查可形成证据；每仓库最多一例。策展者可以核验修复元数据，但 runner 永远不能读取修复 PR、diff 或修复后源码。

## 已锁定的 6/6 分割

对 `reposcope-v1|owner/repo#issue` 计算 SHA-256 并升序排列，前 6 个进入 development，后 6 个进入 hidden。该映射在模型运行前生成，不能按成绩调整：

| Slot | Case |
| --- | --- |
| dev-01 | `pyinvoke-invoke-issue-533` |
| dev-02 | `tox-dev-platformdirs-issue-207` |
| dev-03 | `hynek-structlog-issue-476` |
| dev-04 | `dateutil-dateutil-issue-926` |
| dev-05 | `pallets-click-issue-2819` |
| dev-06 | `pallets-flask-issue-2267` |
| hidden-01 | `pallets-jinja-issue-1198` |
| hidden-02 | `pydantic-pydantic-settings-issue-441` |
| hidden-03 | `python-hyper-h11-issue-92` |
| hidden-04 | `pytest-dev-pluggy-issue-544` |
| hidden-05 | `python-poetry-tomlkit-issue-261` |
| hidden-06 | `more-itertools-more-itertools-issue-658` |

若仓库、Issue 或快照失效，应废弃整个 case 并提升数据集版本，不能为了改善成绩替换个别案例。

## 确定性指标

对 gold 集合 `G` 与去重后的有序预测 `P`：

- `FileRecall@5 = |G ∩ P[:5]| / |G|`
- `MRR = 1 / 首个命中 G 的一基排名`，未命中为 `0`
- 失败 case 的 FileRecall@5 与 MRR 均为 `0`
- 引用只有在 commit、路径、行区间和 excerpt 与 pre-fix 快照完全一致时才有效
- `citation_validity = valid / emitted`
- `citation_hallucination_rate = (emitted - valid) / emitted`
- 没有引用时两个引用比例为 `null`，不能报告 100%

宏平均保留六位小数。只有 Token 数据和明确版本化 rate card 同时存在时才可估算成本。

## 无模型验证命令

```powershell
.\.venv\Scripts\python.exe -m app.evaluation.cli validate-candidates evals\curation-candidates.v1.jsonl
.\.venv\Scripts\python.exe -m app.evaluation.cli validate-dataset --candidates evals\curation-candidates.v1.jsonl --cases evals\benchmark-cases.v1.jsonl --slots evals\benchmark-slots.v2.jsonl --development-gold evals\development-gold.v1.jsonl
.\.venv\Scripts\python.exe -c "from pathlib import Path; from app.evaluation.schemas import export_schemas; assert not export_schemas(Path('evals/schemas'), check=True)"
```

策展者可在本机追加 `--hidden-gold local\evaluation\hidden-gold.v1.jsonl --snapshots-root local\evaluation\snapshots`，验证全部 12 个 snapshot digest 与 gold 文件。该命令仍不会调用模型。

正式在线预测与离线评分必须同时传入完整 12-case catalog 和 `--dataset-digest-file evals\benchmark-cases.v1.sha256`；减少案例、移动 split 或修改字段都会在加载模型凭据前失败。在线进程只接受 `--split development`，没有 gold 参数，并用 Python audit hook 阻止读取 development/hidden gold。两组预测和 SHA-256 manifest 完成后，进程退出；此后才运行只接受 `development-gold.v1.jsonl` 的离线 scorer。

## DeepSeek Flash 真实 development 运行

固定配置为 DeepSeek 官方 `https://api.deepseek.com`、`deepseek-v4-flash`、thinking enabled、`reasoning_effort=low`、temperature 省略、每次最多 16384 输出 Token、总计最多 2,500,000 Token、12 个工具调用、2 个证据轮次和 2 次模型重试。真实预运行表明 `high` 会在简单结构化任务上连续占满 4096 和 8192 输出上限；改为 `low` 后，一个复杂证据批判仍使用了 8043 reasoning token，8192 上限不足以留下完整 JSON 空间。因此正式评分运行统一固定为 `low` 与 16384 上限，费用仅按实际使用量计算。费率卡固定为 `deepseek-v4-2026-08-16-v1`，实际账单仍以 DeepSeek 平台为准。

API Key 只读取当前进程的 `REPOSCOPE_DEEPSEEK_API_KEY` 环境变量，不读取 CLI、结果文件或 run config。不要把 Key 粘贴到聊天、命令参数、README 或 Git。Windows 用户可在“编辑账户的环境变量”中新增该变量，然后重启终端/Codex 让新进程继承。

在 `backend` 作为 Python 工作目录、仓库根目录作为当前目录时执行：

```powershell
.\.venv\Scripts\python.exe -m app.evaluation.online_run --split development --cases evals\benchmark-cases.v1.jsonl --dataset-digest-file evals\benchmark-cases.v1.sha256 --snapshots-root local\evaluation\snapshots --output-directory evals\runs\deepseek-v4-flash-development-v1

.\.venv\Scripts\python.exe -m app.evaluation.development_score --cases evals\benchmark-cases.v1.jsonl --dataset-digest-file evals\benchmark-cases.v1.sha256 --snapshots-root local\evaluation\snapshots --prediction-directory evals\runs\deepseek-v4-flash-development-v1 --development-gold evals\development-gold.v1.jsonl --output evals\runs\deepseek-v4-flash-development-v1\summary.v2.json
```

第一条命令先做五种合成 Schema 兼容性预检，再按每个 case 的 Issue-only → RepoScope 顺序运行；预检不计入两组成绩，但保留在 call ledger。结构化 Schema 错误最多重试 2 次，失败尝试仍计入用量；连续失败或非重试型错误会终止整轮。第二条命令验证 prediction manifest 和所有 artifact digest 后才读取 development gold，全程不会调用模型。`hidden` 和 `all` 不被在线命令接受。

以上命令对应保留的 v1 运行；最新真实结果位于 `evals/runs/deepseek-v4-flash-development-v2/`，其 `reproduce.txt` 保存对应命令。复跑必须换用新的独立目录，不能覆盖历史产物。v2 在评测中拒绝实时 related issues / recent commits，以免当前历史正文污染冻结输入；这与证据传递修复同时改变，不能视作单变量实验。两轮效果均未达到 hidden 门槛；不得把 `null` 引用比例描述为 100% 有效，也不得宣称 RepoScope 优于 Issue-only。

原有 `app.evaluation.cli run` 仍是确定性的 scripted fixture runner，用于无模型测试输入隔离、结果契约和评分管线；其输出不是模型成绩。
