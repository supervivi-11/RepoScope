# DeepSeek V4 Flash development 评测（v1）

## 结论

2026-09-02，RepoScope 在锁定的 6 个 development case 上完成了真实、成对评测。结果不支持进入 hidden 集：Issue-only 的 `FileRecall@5` 为 `0.666667`，RepoScope 为 `0.000000`，差值为 `-0.666667`。RepoScope 的 6 份报告均降级为 `insufficient_evidence`，没有可评分文件或引用。

这些是失败结果，不应包装成“RepoScope 优于基线”。它们说明在线流程、安全隔离和确定性评分已经跑通，也暴露了调查证据没有稳定进入最终报告的产品缺陷。

## 固定输入与配置

- Run ID：`20260902T090635Z-deepseek-v4-flash`
- 源码提交：`6c85a21`
- 数据集 SHA-256：`de76c423ffe936743f979f35cc634caed064e571b3f5c9563b219ac5025ceea7`
- 配置 SHA-256：`6711216c4cb8c803732356ea97dd8dc9d7d7cc90379fec896695914038b6c7f3`
- Provider：DeepSeek 官方 Responses API
- 请求模型：`deepseek-v4-flash`；所有可验证响应均返回同一模型，`provider_backend_drift=false`
- 实际 Python：`3.14.6`；依赖版本以 prediction manifest 为准。该版本高于项目最低要求 3.12，属于复现环境差异，不能宣称这是 Python 3.12 上的实测结果
- Thinking：enabled；reasoning effort：low；temperature：省略
- 单次输出上限：16,384 Token；全局上限：2,500,000 Token
- 每案例最多 12 次只读工具、2 轮证据补查；每个结构化调用最多重试 2 次

预测进程不能读取 development/hidden gold。只有两组预测全部结束并生成带哈希的 manifest 后，独立 scorer 才读取公开 development gold。Hidden gold 未读取、未运行、未提交。

## 聚合结果

| 指标 | Issue-only | RepoScope | 差值 |
| --- | ---: | ---: | ---: |
| FileRecall@5 | 0.666667 | 0.000000 | -0.666667 |
| MRR | 0.583333 | 0.000000 | -0.583333 |
| 引用数 | 0 | 0 | — |
| 引用有效率 | null | null | — |
| 幻觉引用率 | null | null | — |
| 中位延迟 | 18.2545 s | 157.1025 s | — |
| 输入 Token | 3,180 | 721,229 | — |
| 输出 Token | 14,545 | 151,236 | — |
| 估算成本 | $0.019398984 | $0.476403448 | — |

预检额外使用 3,243 输入 Token、1,003 输出 Token，估算 $0.001496736。正式成功运行合计 727,652 输入 Token、166,784 输出 Token（894,436 Token），估算 $0.497299168。费用按版本化 DeepSeek 费率卡估算，实际账单以供应商平台为准。

`null` 引用比例表示没有发出引用，不能解释成 100% 有效或 0% 幻觉。

## 逐案例结果

| Case | Gold 文件 | Issue-only 首次命中排名 | RepoScope | 工具调用 |
| --- | --- | ---: | --- | ---: |
| `dateutil-dateutil-issue-926` | `dateutil/tz/tz.py` | 1 | 无预测、证据不足 | 6 |
| `hynek-structlog-issue-476` | `src/structlog/_log_levels.py` | 未命中 | 无预测、证据不足 | 6 |
| `pallets-click-issue-2819` | `src/click/core.py` | 1 | 无预测、证据不足 | 12 |
| `pallets-flask-issue-2267` | `flask/app.py` | 1 | 无预测、证据不足 | 9 |
| `pyinvoke-invoke-issue-533` | `invoke/tasks.py` | 2 | 无预测、证据不足 | 3 |
| `tox-dev-platformdirs-issue-207` | `src/platformdirs/unix.py` | 未命中 | 无预测、证据不足 | 10 |

## 运行稳定性与失败尝试

成功运行包含 5 次 `schema_error`，均在同案例、同系统、同阶段的下一次尝试中恢复；所有失败尝试的 Token、延迟和费用都保留在账本。该现象说明 Flash 的严格结构化输出需要有界重试，不能把首次解析失败静默丢弃。

在成功运行前还有三次未评分的安全中止：

| 中止原因 | 调用数 | Token | 估算成本 |
| --- | ---: | ---: | ---: |
| 报告结构化输出失败 | 21 | 153,688 | $0.079207744 |
| 工具选择结构化输出失败 | 50 | 414,723 | $0.238581304 |
| 模型调用完成后，旧验证器拒绝含成功重试的账本 | 87 | 852,721 | $0.495645544 |

三次中止均未读取 gold、未生成分数，产物留在 Git 忽略的本地失败目录。成功运行与这三次中止合计估算 $1.310733760；更早的兼容性诊断不计入该数字。

## 失败分析与下一轮门槛

可以确定的事实：RepoScope 每例调用了 3–12 次只读工具，但最终 6 份报告全部没有保留可评分文件或引用。当前结果文件不保存原始模型响应或隐藏思维链，因此不能从这些产物确定究竟是模型主动判为证据不足，还是候选引用在确定性校验时被拒绝。

下一轮开发集调优前应先增加不含思维链的可审计诊断：工具事件、模型选择的证据 ID、引用拒绝码和降级原因。随后将“让模型重写完整 excerpt”改成“让模型选择确定性 evidence ID，再由程序装配原文”，并加入回归测试。只有 development 至少达到 `FileRecall@5 >= 0.70`、相对 Issue-only 提升至少 0.20、引用有效率 100%、幻觉引用率 0%，才考虑一次性运行 hidden 集。

本轮没有达到这些门槛，hidden 集保持未运行。

## 可复现产物

机器可读产物位于 `evals/runs/deepseek-v4-flash-development-v1/`：

- `run-config.json`
- `prediction-manifest.v1.json`
- `issue-only.results.v1.jsonl`
- `reposcope.results.v1.jsonl`
- `call-usage.v1.jsonl`
- `summary.v2.json`
- `reproduce.txt`

复现需要自行提供 `REPOSCOPE_DEEPSEEK_API_KEY`，源码快照仍按安全策略保留在 Git 忽略的本地目录，不随仓库发布。`reproduce.txt` 保存原始命令；由于 runner 会拒绝覆盖已经存在的正式目录，复跑时应把两条命令的输出/预测目录同时替换为新的 Git 忽略目录。
