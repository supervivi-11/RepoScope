# RepoScope

RepoScope 将公开 Python GitHub Issue 转换为有证据支撑的静态排查报告。v1 只做静态、只读分析，**绝不执行仓库代码**，也不会修改仓库内容、创建提交或 PR。

## English summary

RepoScope turns a public Python GitHub issue into an evidence-backed investigation report. Version 1 performs static, read-only analysis only and never executes repository code.

## 本地运行

需要 Python 3.12+、Node.js 24+ 与 Docker Compose。

```powershell
Copy-Item .env.example .env
docker compose up --build
```

API 健康检查：`http://localhost:8000/health`；前端：`http://localhost:5173`。

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
.\.venv\Scripts\python.exe -m pytest backend/tests -q
Set-Location frontend
npm test
npm run build
```

> `.env` 只用于本地配置，已被 Git 忽略。请勿提交任何密钥。
