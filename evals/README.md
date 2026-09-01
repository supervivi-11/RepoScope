# RepoScope v1 评测契约

## 当前真实性状态

- `benchmark-slots.v1.jsonl` 只有 12 个 metadata-only 空槽：`dev-01`–`dev-06` 与 `hidden-01`–`hidden-06`。
- 槽位尚未绑定仓库、Issue、修复 PR 或答案，状态统一为 `unfilled`。
- `curation-candidates.v1.jsonl` 当前包含 6 个通过初步人工核验的 pre-split candidate；它们没有 `split`、slot 或任何修复答案字段，不能作为 benchmark case 运行。
- `results-empty.v1.jsonl` 是零字节结果模板。
- 当前没有真实模型运行、结果或 benchmark 分数；目标值不是已实现成绩。

这样设计是刻意的：未经人工核验就填写“历史 Bug”会制造不可复现数据。候选必须先冻结 runner-safe 的 Issue 与 pre-fix 快照；修复 PR、changed paths、gold 与策展笔记只保存在被 Git 忽略的 `local/evaluation/`。选满 12 个以前，候选不能提前获得 development/hidden 身份。

### `reposcope.eval.candidate.v1`

候选包含稳定 candidate ID、公开仓库 URL、冻结的 Issue 标题/正文、pre-fix commit 与快照 digest。状态只能是 `qualified_pending_dataset_lock`。它不包含 split、slot、case ID、修复 PR、fix commit、changed paths 或 gold。

候选目录允许 1–12 条且每个仓库最多一条。`validate-candidates` 只验证契约，不会调用 runner、模型或 scorer。当前六个候选是：

- [`hynek/structlog#476`](https://github.com/hynek/structlog/issues/476)；
- [`pallets/click#2819`](https://github.com/pallets/click/issues/2819)；
- [`pallets/flask#2267`](https://github.com/pallets/flask/issues/2267)；
- [`pallets/jinja#1198`](https://github.com/pallets/jinja/issues/1198)；
- [`pytest-dev/pluggy#544`](https://github.com/pytest-dev/pluggy/issues/544)；
- [`python-hyper/h11#92`](https://github.com/python-hyper/h11/issues/92)。

这六个名称表示“待全集锁定的候选”，不是开发集案例，也不是评测成绩。

## 四类数据必须物理分离

```text
case.v1 JSONL ──> Issue-only / RepoScope runner ──> result.v1 JSONL

gold.v1 JSONL ───────────────────────────────────> scorer ──> summary.v1 JSON
```

Runner 只能接收 case。Gold 只在模型调用全部结束后交给 scorer。禁止先加载包含答案的“大对象”再靠约定忽略字段。

### `reposcope.eval.case.v1`

包含 case ID、split、仓库 URL、冻结的 Issue 标题/正文、pre-fix commit SHA 与快照树 SHA-256。它不包含修复 PR、fix commit、diff、修改文件或 gold 文件。

### `reposcope.eval.gold.v1`

只包含 case ID 与 1–5 个确定性 gold 生产源码路径。Hidden gold 不进入公开仓库。

### `reposcope.eval.result.v1`

包含系统名、成功/失败状态、有序预测文件、完整引用、安全错误码、runner/model 标识、case 数据集 digest，以及可空的延迟、Token、成本、工具调用和模型尝试数。没有实测值时必须是 `null`，不能用 `0` 冒充。

### `reposcope.eval.summary.v1`

包含每个系统的 case 覆盖数、宏平均 FileRecall@5、MRR、有效/幻觉引用数量与比例，以及在两个系统 case 完全配对时的差值。只有所有 case 都有实测 usage 时才汇总延迟、Token 和成本。

版本化 JSON Schema 位于 `evals/schemas/`。JSONL 使用 UTF-8、每行一个对象、排序键、紧凑分隔符与末尾换行；读取器拒绝 BOM、重复 JSON key、NaN/Infinity、重复 ID、过大文件和非规范路径。

## 十二个历史 Bug 的选择条件

每个槽位只能绑定一个通过人工复核的真实历史 Python Bug：

1. 公开 GitHub Python 仓库，满足 RepoScope 50 MB 仓库、10 MB 索引、500 KB 单文件限制；
2. 已关闭 Issue，存在已合并、单一目的的修复；
3. 固定 Issue 文本与修复前 commit；
4. 修复范围不超过 8 个文件；
5. gold 只取修复中已在 pre-fix 快照存在的 1–5 个 `.py`/`.pyi` 生产文件；
6. 排除测试、文档、生成物、vendor、重命名、新增/删除文件和宽泛重构；
7. Issue 文本不得直接泄漏补丁、修复路径或修复后答案；
8. 静态调查足以形成合理证据，不依赖执行目标仓库代码；
9. 每个仓库最多一个 case，避免仓库风格泄漏；
10. 策展记录可以查看修复元数据，但 runner 输入绝不能读取修复 PR、diff、fix commit 内容或修复后源码。

## 6/6 分割规则

先选满 12 个合格 case，再在任何模型运行前锁定分割。对规范字符串 `reposcope-v1|owner/repo#issue` 计算 SHA-256，按 digest 升序排列：前 6 个为 development，后 6 个为 hidden。结果产生后不得移动 case。

Development gold 可在调试完成后公开；hidden gold 始终由独立 evaluator 保管。若仓库、Issue 或快照失效，应废弃整个 case 并重新锁定数据集版本，不能根据成绩替换。

## 指标定义

对 gold 集合 `G` 与去重后的有序预测 `P`：

- `FileRecall@5 = |G ∩ P[:5]| / |G|`
- `MRR = 1 / 首个命中 G 的一基排名`，未命中为 `0`
- 失败 case 的 FileRecall@5 与 MRR 均为 `0`，防止幸存者偏差
- 引用有效：commit、规范路径、行区间与 excerpt 全部和 pre-fix 快照完全一致
- `citation_validity = valid / emitted`
- `hallucinated_citations = emitted - valid`
- `citation_hallucination_rate = hallucinated / emitted`
- 没有引用时两个引用比例为 `null`，不能报告 100%

所有宏平均保留六位小数。只有 Token 数据与明确版本化 rate card 同时存在时，结果才能包含估算成本。

## 命令

```powershell
# 只验证 12 个空槽，不运行模型
.\.venv\Scripts\python.exe -m app.evaluation.cli validate-slots evals\benchmark-slots.v1.jsonl

# 验证当前 pre-split candidates；不分组、不运行模型
.\.venv\Scripts\python.exe -m app.evaluation.cli validate-candidates evals\curation-candidates.v1.jsonl

# 检查 Schema 是否与 Pydantic 契约一致
.\.venv\Scripts\python.exe -c "from pathlib import Path; from app.evaluation.schemas import export_schemas; assert not export_schemas(Path('evals/schemas'), check=True)"

# 真实策展完成后：runner 与 scorer 分开执行
.\.venv\Scripts\python.exe -m app.evaluation.cli run --help
.\.venv\Scripts\python.exe -m app.evaluation.cli score --help
```

`run` 当前只接受显式 scripted prediction，保证 Task 7 测试不连接 GitHub、模型、PostgreSQL 或任意网络。`IssueOnlyRunner` 只向 predictor 传仓库与冻结 Issue；`RepoScopeRunner` 验证本地 pre-fix 快照 digest，把副本放进 runner 自有临时目录，并在成功、异常或取消时清理。接入真实 provider 属于后续受控实验，不能把 scripted 输出当作模型成绩。
