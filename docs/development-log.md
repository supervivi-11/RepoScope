# 开发过程日志（recall-repair-v1）

本文件是 `codex/recall-repair-v1` 开发周期的关键步骤与排查记录，append-only。只记关键步骤以节省篇幅：

- **阶段条目**：每阶段收尾 1 条（≤10 行）：阶段、日期、commit、关键命令与结果摘要、产物位置。
- **排查条目**：仅当出现明显运行时错误/指标异常时追加，固定模板：现象 → 排查路径 → 根因 → 修复 → 验证 → 遗留风险。
- **付费运行条目**：run id、案例范围、Token/成本、指标变化，细节链接正式评测文档，不复制内容。
- 不记常规成功操作与可从 git log / 评测文档直接读到的内容；推测须标注"推测"。

---

## 2026-09-03 基线条目（阶段 0）

- 分支：`codex/recall-repair-v1`，基于 `36cbf54`（= origin/main `ac8bd8b` + PR8/9 合并验证文档）。fetch 确认远端无新提交。
- 工作位置：`.worktrees/reposcope-v1`（主工作区留在 `92f9251`，收尾时同步）。
- 环境就位：`.venv` Python 3.14.6；`frontend/node_modules` 已存在；Node v24.9.0；Docker 29.4.2 可用。
- 数据就位：`local/evaluation/snapshots` 含全部 12 个冻结快照；`evals/development-gold.v1.jsonl` 在库内；hidden gold 仅在 `local/`（本轮不触碰）。
- 问题基线：真实 DeepSeek development 评测 v1 RepoScope FileRecall@5 = 0.0（0 引用）；v2 = 0.1667，5/6 案例 `insufficient_evidence`，低于同轮 issue-only 基线 0.5。PR 9 已加入逐节点安全诊断，但尚无真实运行诊断数据。
- 本轮目标：程序端到端正常运行 + 新一轮 development 运行 FileRecall@5 > 0、≥4/6 案例有效引用、引用有效率 100%；争取 ≥ issue-only 基线。
- 本轮约定：不做逐次 review，测试为门槛，运行时报错才深入审计；付费模型运行前逐次向用户确认预算；不覆盖已发布 v1/v2 产物。

---

## 2026-09-03 阶段 1：可运行性验证 —— 全部通过

- 后端：`pytest -W error -q backend\tests` → **416 passed / 1 skipped**（与文档基线一致）；`compileall` exit 0。
- 评测 CLI 离线：`validate-slots`（12 槽）、`validate-candidates`（12 候选）、schema 漂移检查 → 全部 exit 0。
- 前端：**65/65** 测试、`typecheck`、`test:build-config`、生产 `build` 全部通过。
- Docker 全栈冒烟（正式路径）：`docker compose up --build --wait` 后 5 服务全部 healthy；主机侧 `/health` 200、`/ready` 200 `{"status":"ready","database":"ok"}`、`/api/v1/demo-cases` 200、前端 `:5173` 200。栈保持运行供后续使用。
- 早期降级冒烟（Docker 未启动时）：uvicorn `/health` 200、`/ready` 503（无库优雅降级，符合预期）、dist 静态服务 200 —— 已被 Docker 全栈结果取代。
- 未执行：playwright e2e（需下载浏览器；Task 8 发布验证已覆盖过）。
- 环境备注：`.env` 从主工作区复制入 worktree（gitignored）；`local/` 确认被 gitignore。

### 排查条目 T1：前端 vite 测试/构建 spawn EPERM

1. 现象：`npm test` 失败，vite 加载 `vite.config.ts` 报 `Error: spawn EPERM at optimizeSafeRealPathSync`。
2. 排查路径：完整堆栈 → vite(rolldown) 用管道 stdio 的 `execFile` 派生子进程解析 realpath → 命中 Windows 沙箱"程序不能打开命名管道"文档化边界。
3. 根因：当时沙箱限制，非项目代码缺陷（会话后段沙箱已放开为 danger-full-access，此问题不再出现）。
4. 修复：按沙箱文档一次性升级重试同一命令；未改代码。
5. 验证：65/65 测试、typecheck、build、test:build-config 全绿。
6. 遗留：无。

### 排查条目 T2：pytest tmp_path 全部 WinError 5（191 passed / 226 errors）

1. 现象：完整 pytest 226 个 ERROR；`tmp_path` fixture 创建时 `PermissionError: [WinError 5]`（basetemp scandir/mkdir 被拒）。
2. 排查路径：
   - `-p no:cacheprovider` + 工作区新 `--basetemp` 仍复现，排除缓存目录问题；
   - 对照实验：PowerShell 建的目录可枚举；Python `os.mkdir` 建的目录（`backend\.pytest_cache`、旧 temp `pytest-of-1`、新 basetemp）全部拒绝枚举/读 ACL/删除；
   - 最小实验（同进程）：mkdir 0o777/0o755 可枚举，**0o700 连创建进程自己都拒绝**；无沙箱重跑同实验：0o700 正常。
3. 根因：Python 3.14 新行为（`os.mkdir` 按 mode 写显式 DACL）× DSH Windows 沙箱文件过滤 → 沙箱内创建的 0o700 目录 ACL 损坏；pytest tmp 机制硬编码 mode=0o700。非项目代码缺陷。
4. 修复：pytest 升级出沙箱运行，并保持 `-p no:cacheprovider --basetemp=local\tmp\pytest`；未改产品/测试代码。
5. 验证：**416 passed / 1 skipped**，与文档基线一致。
6. 遗留：沙箱期遗留的损坏 ACL 目录（`backend\.pytest_cache`、根 `.pytest_cache`、`local\tmp\probe\d700` 等）待权限放开后清理；均 gitignored，flags 已绕开。

### 排查条目 T3：compose 栈 8000 端口冲突 + api 持续 unhealthy（/ready 503）

1. 现象：`compose up` 报 `Bind for 0.0.0.0:8000 failed: port is already allocated`；处理后再 up，api 容器 unhealthy，`/ready` 持续 503。
2. 排查路径：
   - `docker ps -a` → 主工作区遗留僵尸容器 `newproject-api-1`（unhealthy，其 postgres 16h 前已退出）占用 8000，`newproject-worker-1` 重启循环 → stop 两者；
   - 仍 503 → api 日志显示 uvicorn 正常、仅 readiness 失败 → 查库：`alembic_version=20260830_0001`、5 张表齐全，与 `readiness.py` 期望完全一致 → 排除 schema 问题；
   - api 容器内直连测试：**DNS 解析不了 `postgres`**（首次 up 端口冲突时 api 网络端点半配置）+ `.env` 的 `POSTGRES_PASSWORD` 与旧卷初始化凭据不一致（postgres 数据目录已存在时忽略 POSTGRES_* 环境变量）。
3. 根因：三层环境残留叠加：僵尸容器占端口；半配置网络端点；旧数据卷凭据与当前 `.env` 不匹配。均非代码缺陷。
4. 修复：`docker compose down -v` 重建 reposcope-v1 栈（卷内仅可重建 schema 与空 snapshots 卷，无有价值数据）；newproject 容器仅 stop 未删除。
5. 验证：5 服务全部 healthy；`/health` 200、`/ready` 200、demo-cases 200、前端 5173 200。
6. 遗留：newproject 旧栈可随时 `docker start` 恢复；8000/5173 现由 reposcope-v1 栈占用。

---

## 2026-09-03 阶段 2：离线召回诊断 —— 损失层已定位（无模型调用，免费）

- 探针：`local/probe_recall.py`（不入库）对 6 个 development 案例的冻结快照建索引，用冻结 Issue 文本的确定性查询（backtick 片段/点分路径/标识符 token/gold 文件名）测 `search_code`/`find_symbol`；结果 `local/probe-results/recall-probe.v1.json`。
- **关键发现：检索层"有能力"——6/6 案例的 gold 文件都可被合理查询召回且排名 1-9**（如 flask `handle_exception`→rank2、click `help_option_names`→rank1）。召回 0 的主通道不在检索层。
- 静态梳理确认四个损失层（详见 `local/probe-results/phase2-loss-layers.md`）：
  - A 检索缺口：纯路径匹配零命中、点分路径 0 命中、频次计分噪声大、Issue 派生 token 零命中率最高 34%；
  - B critique 无判定标准：`sufficient` 无描述、与工具选择共用指令、仅 2 轮 → 提前放弃（v2 structlog 只用 3 次工具）；
  - **C 引用精确性瓶颈（recall=0 的唯一清空通道）**：主假设引用必须精确 (commit,path,start,end)，任何漂移 → 验证拒绝 → 整报降级清空 impacted_files → predicted_files 为空；
  - D 上下文膨胀：每次调用重发全部证据全文（v2 ≈35K token/调用），放大抄写失败率。
- 阶段 3 判别目标：用 diagnostics 区分各案例主导损失是 B（提前 insufficient）还是 C（rejection codes）还是 A（零命中占比）。
- 环境备注：损坏 ACL 的 `.pytest_cache` 在完全访问下仍无法删除（owner 为沙箱 SID，需管理员 takeown）；已被 pytest flags 永久绕开，无功能影响。

---

## 2026-09-03 阶段 3：诊断工具落地 + 单案例付费诊断运行

**实现**（commit `167c460`）：`online_run` 新增 `--case`（1–5 个，可重复）进入部分诊断分支——保留全部输入校验/gold 读取防护/预检/逐案例结果校验/失败即中止/诊断落盘校验；免除六例强制、manifest、complete-evidence 校验与干净源码树要求；新增 `diagnostic-run.v1.json` 标记（明示"非 benchmark 产物、不可评分"）；`validate_diagnostic_artifact` 增加 `allow_partial`（默认路径不变）。新增 15 项定向测试；全套 431 passed / 1 skipped；schema 无漂移。

**付费运行条目**（用户确认后执行）：
- Run ID `20260903T085620Z-deepseek-v4-flash-diagnostic`；案例 `pallets-flask-issue-2267`；产物 `local/evaluation/diagnostics-20260903-flask/`。
- 用量：116,126 输入 / 20,670 输出 Token，**$0.074945**（含预检），133s，4 次工具、8 次模型调用全部成功、无 schema 重试。
- **诊断确认（C 层为该案例唯一损失通道）**：工具全部成功（map 32 引用、search 20+4、read 1，证据池 61 条）→ critique 第一轮 `sufficient=True`（2 假设/6 引用，仅 2 个与工具读取位置精确匹配）→ compose 产出 **`root_cause_identified` + 1 个受影响文件** → validate 将全部 5 条引用以 `excerpt_mismatch` 拒绝 → `primary_unvalidated` → 整报降级清空（证据 61→0）→ `predicted_files=[]` → recall=0。
- 结论：模型找到了根因，但引用转写不精确导致全部清空；B 层（critique 标准）本案例未构成阻塞；D 层（约 14K token/调用的重复上下文）是转写漂移放大器。
- 阶段 4 修复方向（由上述证据确认）：① C——有效位置的 excerpt 漂移由"拒绝"改为"重锚定"（以快照原文确定性替换；commit/source mismatch 仍拒绝）；② D——上下文去重（tool_history 不再重复携带全文引用）；③ B——critique 增加明确充分性标准；④ 报告指令改为"位置必须精确、excerpt 可留空由系统补齐"。A 层（检索缺口）本轮缓修：flask 案例检索未受阻，探针证据留档。

---

## 2026-09-03 阶段 4（批 1）：C 层重锚定修复 + D/B 辅修

**修复**（commit `7485ad4`，全部基于阶段 3 诊断实证）：
- **C（主）**：`bind_tool_evidence` 扩展——显式引用位置与"工具读取且重校验通过"的证据条目精确匹配但 excerpt 漂移时，以规范快照原文替换（保留模型 explanation）；伪造文本从不持久化；未在工具池验证过的位置仍交给验证器拒绝。`validate_evidence` 零改动（commit/source/excerpt mismatch 拒绝语义不变）。注：指令中"excerpt 可留空"不可行——schema 强制非空，改为"verbatim 复制 + 系统重锚定"双保险。
- **D（辅）**：调查上下文不再于 tool_history 重复携带全文引用（证据池已含同一内容）。
- **B（辅）**：指令与 `CritiqueResult.sufficient` 描述加入充分性标准（位置合理解释症状即充分，疑虑记入 uncertainties）；检索零命中时换更短/不同标识符重试的策略提示。
- 报告指令：精确复制证据池位置与 excerpt、只引用已出现范围、更宽范围先 read_code。

**测试契约更新**（设计变更，非削弱）：① 已验证位置漂移 → 规范替换、报告存活、伪造文本不落盘（新增单测 + graph 级测试改写 + predictors 新增 `resourced_excerpt` 场景）；② `excerpt_mismatch` 拒绝覆盖改用未读位置 (2,2)，`bad_excerpt` 场景期望不变；③ unknown/wrong_sha/bad_tool "永不修复"测试保留原样。全套 **432 passed / 1 skipped**；schema 无漂移。

**A 层缓修理由**：flask 案例检索未受阻（map 32 引用 + search 24 + read 1 全部成功），阶段 2 探针证据留档，v3 正式运行后按数据再评估。

**待验证**：flask-2267 单案例诊断复跑（需用户预算确认，预计 <$0.15）；期望 `report_reason: primary_unvalidated → supported_primary`、`predicted_files` 非空。

---

## 2026-09-03 阶段 4（验证）：修复后 flask-2267 付费复跑——案例完全翻转

**付费运行条目**（用户确认后执行）：
- Run ID `20260903T092453Z-deepseek-v4-flash-diagnostic`；产物 `local/evaluation/diagnostics-20260903-flask-fix/`（诊断标记，非 benchmark）。
- 用量：15 次调用（含预检），94,442 输入 / 16,765 输出 Token，**$0.062297**，117s，4 次工具 / 9 次模型调用，全部成功。

**结果对比（同案例、同命令、修复前后）**：

| 指标 | 修复前 (085620Z) | 修复后 (092453Z) |
|---|---|---|
| report_reason | primary_unvalidated | **supported_primary** |
| outcome | insufficient_evidence | **root_cause_identified** |
| predicted_files | `[]` | **`['flask/app.py']`** |
| 验证通过引用 / 拒绝 | 0 / 5×excerpt_mismatch | **3 / 0** |
| 输入 Token / 案例成本 | 112,208 / $0.0719 | **94,442 / $0.0581** |

- 对照公开 dev gold（`gold_files=['flask/app.py']`）：**FileRecall@5 = 0.0 → 1.0**。
- 机制确认：模型按新指令精确复制证据池位置（flask/app.py 1486–1511 三个范围），引用全部通过快照校验；上下文去重降低约 16% 输入 Token。
- 累计诊断支出：$0.1372（两次单案例），均在用户逐次确认的预算内。

**结论**：C 层修复在真实模型上生效。下一步（阶段 5，需用户预算确认 ≤$0.8）：正式 6 案例 v3 运行 → `evals/runs/deepseek-v4-flash-development-v3/` → development_score → 评估文档 → PR。

---

## 2026-09-03 阶段 5：正式 6 案例 v3 运行——开发集达标

**付费运行条目**（用户确认 ≤$0.8 后执行）：
- Run ID `20260903T095547Z-deepseek-v4-flash`；产物 `evals/runs/deepseek-v4-flash-development-v3/`（新目录，v1/v2 未覆盖）；clean source commit `30eb3c8`，working_tree_clean=true。
- 用量：91 次调用（84 成功、7 次 schema_error 均在重试内恢复），817,359 输入 / 170,523 输出 Token，**$0.522130**（含预检 $0.0016），20 分 24 秒。
- 离线评分（免费，独立进程读 gold）：RepoScope FileRecall@5 = MRR = **0.666667**（v2 为 0.166667），4/6 案例 `root_cause_identified` 且首命中均排名 1；**20/20 引用有效、幻觉率 0**；本轮 Issue-only 0.5 / 0.375，差值 +0.167 / +0.292。
- **验收核对**：recall>0 ✓；≥4/6 案例有效引用 ✓（恰 4/6）；引用有效率 100% ✓；幻觉 0% ✓；≥ issue-only 基线 ✓。**Hidden 门槛未过**（0.667<0.70、+0.167<+0.20）→ hidden 不运行、不宣称产品效果达标。
- 残余（详见 v3 文档"残余限制"）：flask 正式运行仍 `primary_unvalidated`（3 条引用已通过校验，但主假设引用池外合成位置；同案例修复后两次单例诊断均命中——生成波动）；invoke 7 次 read_code 失败 + 假设引用零匹配。下一迭代候选：位置级重锚定（需安全评审）、read_code 错误信息引导、A 层检索缺口。
- 评估文档 `docs/evaluations/deepseek-v4-flash-development-v3.md`；本任务累计付费支出 **$0.659**（两次诊断 $0.137 + v3 $0.522），全部经用户逐次确认。
