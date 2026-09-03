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
