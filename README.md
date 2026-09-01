# RepoScope

RepoScope 是一个面向陌生 Python 仓库的 Bug 调查助手：输入公开 GitHub 仓库与 Issue 编号，它在固定 commit 的源码快照上进行静态、只读调查，并输出可校验的根因候选、影响文件、修改步骤与测试建议。

> 当前状态：v0.1.0 候选版本仍在开发中。实时分析只供本地运行；公网内容仅回放三个明确标注的产品流程占位 Demo。它们不是历史案例、真实模型结果或 benchmark 成绩。项目尚未运行真实模型评测，因此**没有可报告的 benchmark 分数**。

## English summary

RepoScope investigates public Python GitHub bug issues against immutable pre-fix source snapshots. It exposes only bounded read-only tools, validates every citation deterministically, and produces a structured report. Live analysis is local-only and uses the operator's OpenAI-compatible credentials. The public demo is static. The committed demo cases and benchmark slots are placeholders, not measured results; no real-model benchmark has been run yet.

## 它解决什么问题

接手陌生仓库时，困难通常不是“写代码”，而是先找到入口、调用关系、相关测试与可信证据。RepoScope 将这个调查过程约束为：

1. 校验 GitHub URL、Issue 与仓库限制；
2. 固定默认分支 commit，安全下载只读源码快照；
3. 用 AST、关键词和静态代码块建立索引；
4. Agent 最多调用 12 次只读调查工具；
5. 在持久化前重新校验 commit、路径、行号与原文；
6. 证据不足时明确返回 `insufficient_evidence`，而不是猜测根因。

RepoScope 不执行目标仓库代码、不安装其依赖、不修改文件，也不创建 commit 或 PR。

## 架构概览

```text
React（静态 Demo / 本地实时模式）
        │ REST + SSE
        ▼
FastAPI ── PostgreSQL 任务、事件、报告
        │
        ▼
Worker ── 安全 GitHub 摄取 ── 固定 commit 快照
        │                         │
        ├── Python 静态索引 ◄─────┘
        └── LangGraph + 只读工具 → 引用校验 → 报告

评测：case JSONL → runner → result JSONL
                     gold JSONL → scorer → summary JSON
```

详细边界与数据流见 [架构文档](docs/architecture.md)。

## 本地启动

要求：Python 3.12+、Node.js 24+、Docker Compose。复制配置后填写自己的密钥；不要提交 `.env`。如果暂时不填写模型 Key，Compose 仍可完成数据库迁移、健康检查和静态界面启动；worker 会进入不领取任务的安全降级状态。填写 Key 后重建 worker 才会处理实时分析。

```powershell
Copy-Item .env.example .env
docker compose up --build
```

- 前端：`http://localhost:5173`
- API 健康检查：`http://localhost:8000/health`

不使用 Docker 时：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".\backend[dev]"
.\.venv\Scripts\uvicorn.exe app.main:app --app-dir backend --reload

Set-Location frontend
npm ci
npm run dev
```

## 测试与构建

```powershell
.\.venv\Scripts\python.exe -m pytest -W error -q backend\tests
.\.venv\Scripts\python.exe -m compileall -q backend

Set-Location frontend
npm test -- --run
npm run typecheck
npm run test:build-config
npm run build
npm run build:live
npm run test:e2e

Set-Location ..
.\.venv\Scripts\python.exe backend\scripts\scan_release_secrets.py --repo-root . --artifact frontend\dist --allowlist security\secret-scan-allowlist.json
```

Task 8 的实际命令、环境与观察结果见 [发布验证记录](docs/release-verification.md)。

## 可复现评测骨架

评测仍只提供离线、无付费调用的契约与 runner。十二个数据槽保持 `unfilled`；当前只有 2 个通过初步人工核验、尚未分组的候选，仍需选满 12 个后才能锁定 6/6 分割。以下验证不会输出成绩：

```powershell
.\.venv\Scripts\python.exe -m app.evaluation.cli validate-slots evals\benchmark-slots.v1.jsonl
.\.venv\Scripts\python.exe -m app.evaluation.cli validate-candidates evals\curation-candidates.v1.jsonl
.\.venv\Scripts\python.exe -c "from pathlib import Path; from app.evaluation.schemas import export_schemas; assert not export_schemas(Path('evals/schemas'), check=True)"
```

完成合规的数据策展后，分别运行两个系统，再把 gold 仅交给 scorer：

```powershell
.\.venv\Scripts\python.exe -m app.evaluation.cli run --system issue_only --cases evals\cases.v1.jsonl --scripted-predictions local\issue-only-script.v1.jsonl --output local\issue-only-results.v1.jsonl
.\.venv\Scripts\python.exe -m app.evaluation.cli run --system reposcope --cases evals\cases.v1.jsonl --scripted-predictions local\reposcope-script.v1.jsonl --snapshots-root local\snapshots --output local\reposcope-results.v1.jsonl
.\.venv\Scripts\python.exe -m app.evaluation.cli score --cases evals\cases.v1.jsonl --gold local\gold.v1.jsonl --results local\results.v1.jsonl --snapshots-root local\snapshots --output local\summary.v1.json
```

这里的内置适配器是确定性的 scripted runner，用来验证输入隔离、结果契约和评分管线；它不声称替代尚未执行的真实模型实验。数据选择、分割、指标定义和防答案泄漏规则见 [evals/README.md](evals/README.md)。

## 安全与贡献

- 安全边界与漏洞报告：[SECURITY.md](SECURITY.md)
- 开发与 PR 约定：[CONTRIBUTING.md](CONTRIBUTING.md)
- 演示录制：[docs/demo-recording.md](docs/demo-recording.md)
- 面试讲解：[docs/interview-guide.md](docs/interview-guide.md)
- 发布检查：[docs/release-checklist.md](docs/release-checklist.md)

许可证：[MIT](LICENSE)。
