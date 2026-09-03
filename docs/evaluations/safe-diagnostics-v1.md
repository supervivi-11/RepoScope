# 安全诊断与合成离线回放（v1）

## 本阶段解决什么

基于 `1c1cf54`（development v2 结果，PR #8），补齐在线评测器原本丢弃逐节点观察记录的缺口。
没有调用真实模型、读取隐藏答案、重跑 benchmark 或更新成绩。历史 v1/v2 产物保持原样。
旧结果没有这些记录，无法事后恢复五例降级原因；本阶段不声称已经修复这些真实案例。

新增 `evals/schemas/diagnostic.v1.schema.json`，未来正式 online runner 自动写入
`diagnostics.v1.jsonl`，无需改变输入参数。它是给开发者读取的旁路产物，不进入模型上下文。
线上产品 API、SSE、报告和 checkpoint 的字段均不改变。

## 观察边界

Graph 使用可选的可信 host observer，在节点函数返回后、状态提交之前观察 input/update；
仅评测器安装 observer，默认生产执行仍没有这个回调。回调不能给模型新增工具。
验证节点直接提供验证器的固定 rejection code 和计数，不从错误文本猜原因，也不重复运行验证。

| 阶段 | 可观察信息 |
| --- | --- |
| 工具执行 | 白名单工具名/unknown、成功与否、返回引用数、证据池前后数量 |
| 证据审查 | sufficient、假设及引用数量、匹配历史成功工具读取的位置数量 |
| 报告生成 | 模型声明 outcome、主假设引用位置 ID、顶层引用 ID、影响文件数量 |
| 报告验证 | 补齐数、有效数、固定拒绝码、最终引用 ID 与保守原因分类 |
| 全程 | 连续序号、节点、工具/轮次/模型调用计数、白名单事件 |

`commit_mismatch`、`source_mismatch`、`excerpt_mismatch` 是确定性引用拒绝码。
报告原因包括模型主动声明证据不足、主假设缺失、主假设没有引用、主引用未通过校验、主引用保留。
这些分类描述程序观察，不代替语义正确性或根因评审。

注意：critique 会替换假设、合并引用，**不会校验并过滤引用**；引用过滤发生在报告验证。
证据池合并有去重和 64 条上限，前后差值不能解释成精确的“被丢弃数量”。
工具成功但返回零引用也不一定没价值（例如目录导航）；有效的非主假设引用也可能被安全降级报告清空。

## 不记录什么

从允许字段正向构建封闭 Schema，不使用“序列化全部 state 再脱敏”。不落盘 Issue 正文、
工具参数、路径原文、源码、excerpts、模型报告文字、假设陈述、uncertainties、异常消息、Key、prompt、响应或隐藏思维链。
未知事件与工具名替换为 `unknown`。引用 ID 仅对 commit/path/start/end 位置元组计算 SHA-256；
不哈希源码或自由文本。这用于关联位置，**不承诺匿名化**，已知位置可计算同一 ID。

## 持久化及失败语义

- 单写者、仅 development、最多六例、每例最多 64 个观察步骤；不支持并行写入或恢复旧日志。
- 每个已完成节点原子替换 JSONL；图的正常 review interrupt 不视为失败。
- 普通模型失败保留前缀并标为 `aborted`；写盘/投影失败安全中止，不继续模型调用，不输出完整 manifest。
- 写盘失败或进程突然退出可能只留下 `in_progress` 前缀；这不是完整结果，不能用于评分。
- 日志记录的是节点函数完成，不是持久 checkpoint 事务成功。失败节点本身不产生成功步骤；已核算 provider ledger 仍用于定位失败调用及其用量。

## 产物兼容与校验

正式新运行要求完整 diagnostics 文件并将其哈希放入 manifest。
Scorer 在读取 development gold 前检查可选诊断文件的哈希、封闭 Schema、六例身份、SHA、
连续序号、节点顺序、证据池连续性、预算计数与最终 prediction 的 outcome/引用/调用数一致性。
未知 manifest 附件仍被拒绝。

**新版 scorer 可以读取没有 diagnostics 的历史 v1/v2 产物；旧版 scorer 程序不能读取新增了第六个附件的 manifest。**
现有 result、summary、manifest 数据 Schema 和指标公式未改；不要往旧目录补写推测的诊断记录。

## 无模型复现

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/evaluation/test_real_predictors.py backend/tests/evaluation/test_development_score.py backend/tests/evaluation/test_online_run_isolation.py -q
.\.venv\Scripts\python.exe -m app.evaluation.diagnostics local/evaluation/your-new-run/diagnostics.v1.jsonl
```

第二条命令需已有诊断文件，只读取其封闭字段并显示案例状态、完成节点数与报告分类；不访问模型、源码或 gold。
本阶段没有真实模型运行，因此也没有真实 diagnostics 产物可供上述第二条命令直接演示。

测试使用临时合成源码和预设模型输出，覆盖启用/禁用诊断时的预测、模型输入和调用数一致性；
引用绑定、坏范围、坏原文、仅 critique 证据、有效非主引用被清空；未知工具、空检索、两轮调查、12 工具上限；
自由文本 canary 不泄漏；模型失败与中途写盘失败前缀保留；附件损坏/字段注入/身份/计数/序号不一致在读答案前拒绝。
这是**合成离线回放**，不是从真实日志重放模型，也不能生成新 benchmark 分数。

本阶段验证：完整后端 416 passed / 1 skipped；前端 65 passed，TypeScript 与静态构建通过；
评测 Schema 无漂移，Git 历史/暂存区/构建产物安全扫描通过，历史 v1/v2 manifest 中的产物哈希逐项一致。
独立审查未发现 Critical / Important / Minor 缺陷，并重新运行 54 项定向测试通过。
仍未直接覆盖诊断开启时的 Schema 重试等价性和真实文件系统 replace 故障；写盘失败测试使用注入故障。

## 后续边界

先审阅并合并代码，再单独确认一个未成功 development 案例的小规模真实诊断预算。
诊断数据齐备后只修复能由证据确认的问题，验证后再决定是否重跑完整开发集。
不自动花钱、不换模型或提高预算、不进入 hidden、不修改指标以掩盖未达标结果。
