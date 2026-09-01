# RepoScope v0.1.0 发布候选验证

验证日期：2026-09-01（Asia/Shanghai）

起始 commit：`a8cc704`

分支：`codex/reposcope-v1`
状态：Task 8 本地发布候选；未 push、未部署、未创建 tag、未发布 Release。

本记录只写实际观察结果。没有运行真实模型或付费 API，没有提交分析任务，没有运行真实 benchmark，也没有生成或暗示 benchmark 分数。

## 环境

- Windows + PowerShell；Docker Desktop 29.4.2 / Compose 5.1.3；
- 本地虚拟环境 Python 3.14.6（项目声明兼容 Python 3.12+）；
- Node.js 24.12.0 / npm 11.6.2；
- Playwright Chromium 151（Playwright browser revision 1234）；
- Docker 项目名固定为 `reposcope_task8`，仅操作其命名容器和卷。

## 后端

```powershell
.\.venv\Scripts\python.exe -m pytest -W error -q backend\tests
.\.venv\Scripts\python.exe -m compileall -q backend\app backend\tests
.\.venv\Scripts\python.exe -m pip check
```

观察结果：`276 passed, 1 skipped in 7.94s`；compileall 退出码 0；`No broken requirements found.`。跳过项是当前环境不具备的符号链接能力分支，不是失败。

## 前端、构建与真实浏览器

```powershell
Set-Location frontend
npm test -- --run --reporter=dot
npm run typecheck
npm run test:build-config
npm run test:e2e
```

观察结果：

- Vitest：11 个文件、65 个测试全部通过；
- TypeScript：退出码 0；
- 构建隔离：`build configuration isolation verified`；
- live build：78 modules，JS 275.34 kB（gzip 84.47 kB），CSS 18.25 kB（gzip 4.74 kB）；
- static build：同等大小，使用独立静态 bundle；
- Chromium live：1 个“创建 → SSE 重连 → 报告 → 接受”场景通过；
- Chromium static：3 个场景通过，包括 Demo 播放零 API/GitHub 请求、hash 刷新与 360px 键盘/溢出检查。

Playwright 自带 `webServer` 在本机 Windows 上无法可靠结束预览进程。发布候选改用 `scripts/run-e2e.mjs` 显式拥有 server 生命周期，测试完成后退出码为 0 且端口不残留。

## 生成物与评测契约

```powershell
.\.venv\Scripts\python.exe backend\scripts\generate_frontend_limits.py --check
.\.venv\Scripts\python.exe -c "from pathlib import Path; from app.evaluation.schemas import export_schemas; raise SystemExit(1 if export_schemas(Path('evals/schemas'), check=True) else 0)"
.\.venv\Scripts\python.exe -m app.evaluation.cli validate-slots evals\benchmark-slots.v1.jsonl
```

观察结果：限制生成器和评测 Schema 均无漂移；目录验证输出 `validated 12 metadata-only slots (6 development, 6 hidden)`。十二个槽位仍为 `unfilled`，未执行真实 runner 或 scorer。

## 发布密钥扫描

```powershell
.\.venv\Scripts\python.exe backend\scripts\scan_release_secrets.py `
  --repo-root . `
  --artifact frontend\dist `
  --allowlist security\secret-scan-allowlist.json
```

观察结果：`release security scan passed: Git history and artifacts contain no unreviewed high-confidence secrets`。

扫描覆盖完整 Git 历史、当前跟踪文件与 `frontend/dist`。发现时只输出来源、路径、行号、规则和截断摘要，不输出匹配值。测试中的合成凭据只按“路径 + 规则 + SHA-256”精确放行。超过 5 MiB 扫描上限的文件或历史 blob 会产生 `scan_limit_exceeded` 并令命令失败，不会被静默跳过。扫描同时拒绝跟踪 `.env`、数据库、构建/Playwright 产物、`evals/gold/` 和本地结果目录。

## Docker Compose、真实 PostgreSQL 与迁移

首次从 Docker Hub 拉取 `pgvector/pgvector:pg16` 时，本机 daemon 配置的第三方 mirrors 两次在 OCI referrers 阶段返回 HTML，Docker 报 `invalid character '<'`。随后显式从已配置的 DaoCloud mirror 拉取同名镜像，返回 index digest `sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b`；该值与当日 Docker Hub `pgvector/pgvector:pg16` 索引一致，再添加本地标准标签。仓库中的 Compose 镜像引用没有改成第三方地址。

最终验证先确认项目名，再只删除 `reposcope_task8` 的旧容器和卷：

```powershell
docker compose -p reposcope_task8 ps --all
docker compose -p reposcope_task8 down --volumes --remove-orphans

$env:GITHUB_TOKEN=''
$env:REPOSCOPE_OPENAI_API_KEY=''
$env:REPOSCOPE_OPENAI_BASE_URL=''
docker compose -p reposcope_task8 up --build --pull never -d --wait --wait-timeout 120
docker compose -p reposcope_task8 config --quiet
docker compose -p reposcope_task8 ps --all
```

观察结果：从新命名卷启动成功；PostgreSQL、API、frontend healthy；migrate `Exited (0)`；worker 在空模型配置下持续 running，`restart_count=0`，日志仅有 `worker_disabled code=model_not_configured`。

数据库与端点检查：

```powershell
docker compose -p reposcope_task8 exec -T postgres psql -U reposcope -d reposcope -Atc `
  "SELECT version_num FROM alembic_version; SELECT string_agg(tablename, ',' ORDER BY tablename) FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'analysis_%'; SELECT default_version FROM pg_available_extensions WHERE name='vector'; SELECT count(*) FROM analysis_jobs;"
docker compose -p reposcope_task8 exec -T api python -c `
  "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health').read().decode()); print(urllib.request.urlopen('http://localhost:8000/ready').read().decode())"
docker compose -p reposcope_task8 exec -T frontend wget -q -O - http://127.0.0.1/
```

观察结果：

- Alembic revision：`20260830_0001`；
- 表：`analysis_events,analysis_feedback_commands,analysis_jobs,analysis_report_versions`；
- pgvector 可用版本：`0.8.6`；
- 空队列：`analysis_jobs = 0`；
- `/health`：`{"status":"ok","service":"reposcope-api"}`；
- `/ready`：`{"status":"ready","database":"ok"}`；
- nginx 返回 RepoScope `<!doctype html>`。

验证中发现 Alpine 的 `localhost` 选择 IPv6、而 nginx 配置只监听 IPv4，造成前端误报 unhealthy；已用失败回归测试固定并把 healthcheck 改为 `127.0.0.1`。也发现空 Key 会让旧 worker 在构造模型客户端时 restart-loop；现在改为不构造客户端、不领取任务的安全降级模式。

## 尚未执行或不构成通过项

- 未运行真实模型、真实历史 12-case benchmark、Issue-only/RepoScope 实验或 scorer；
- 未策展 12 个真实历史 Bug，6/6 仍只是锁定的 metadata-only 槽位；
- 三个公开 Demo 仍是显著标注的产品流程占位数据，不是真实历史导出；
- 未制作 60 秒 GIF 或 3–5 分钟视频；仓库仅提供录制与泄密检查指南；
- 未验证真实模型供应商、真实 GitHub 限流或付费成本；
- 未 push、部署、打 tag 或发布 GitHub Release。

这些未完成项不能被 README、简历或 Release 文案描述为成绩或已发布能力。
