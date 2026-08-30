import { useMutation } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";

import { ApiError, type ApiClient } from "../api/client";
import { bundledDemos } from "../demo/artifacts";
import { analysisHref, demoHref } from "../router";

function validRepoUrl(value: string): boolean {
  try {
    const parsed = new URL(value);
    const parts = parsed.pathname.replace(/\/$/, "").split("/").filter(Boolean);
    return (
      parsed.protocol === "https:" &&
      parsed.hostname === "github.com" &&
      parsed.username === "" &&
      parsed.password === "" &&
      parsed.search === "" &&
      parsed.hash === "" &&
      parts.length === 2 &&
      parts.every((part) => /^[A-Za-z0-9_.-]+$/.test(part))
    );
  } catch {
    return false;
  }
}

function generatedIdempotencyKey(): string {
  const identity = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `reposcope-${identity}`;
}

export function Landing({ apiClient, liveEnabled }: { apiClient: ApiClient; liveEnabled: boolean }) {
  const [repoUrl, setRepoUrl] = useState("");
  const [issueNumber, setIssueNumber] = useState("");
  const [useIdempotency, setUseIdempotency] = useState(true);
  const [validation, setValidation] = useState<{ repo?: string; issue?: string }>({});
  const intent = useRef<{ signature: string; key: string } | null>(null);
  const create = useMutation({
    mutationFn: ({ repo, issue, idempotencyKey }: { repo: string; issue: number; idempotencyKey?: string }) =>
      apiClient.createAnalysis(
        { repo_url: repo, issue_number: issue },
        idempotencyKey,
      ),
    onSuccess: (result) => {
      intent.current = null;
      window.location.hash = analysisHref(result.analysis_id);
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!liveEnabled) return;
    const issue = Number(issueNumber);
    const nextValidation: { repo?: string; issue?: string } = {};
    if (!validRepoUrl(repoUrl.trim())) nextValidation.repo = "请输入公开 GitHub 仓库 URL。";
    if (!Number.isSafeInteger(issue) || issue <= 0) nextValidation.issue = "Issue 编号必须是正整数。";
    setValidation(nextValidation);
    if (Object.keys(nextValidation).length > 0) return;
    const repo = repoUrl.trim();
    const signature = `${repo}\n${issue}`;
    if (intent.current?.signature !== signature) {
      intent.current = { signature, key: generatedIdempotencyKey() };
    }
    create.mutate({ repo, issue, idempotencyKey: useIdempotency ? intent.current.key : undefined });
  }

  const createError = create.error instanceof ApiError
    ? create.error.message
    : create.error
      ? "创建分析失败，请确认本地服务已启动。"
      : null;

  return (
    <main id="main-content" tabIndex={-1}>
      <section className="hero page-shell">
        <div className="hero-copy">
          <p className="eyebrow">STATIC · READ-ONLY · EVIDENCE-FIRST</p>
          <h1>把陌生仓库，变成可验证的调查路径。</h1>
          <p className="hero-subtitle">Evidence-first investigation workspace for public Python bug reports.</p>
          <p className="hero-boundary">只读取公开 GitHub 仓库；不会执行仓库代码、安装依赖、修改文件或创建 PR。</p>
        </div>
        <aside className="hero-aside" aria-label="RepoScope v1 边界">
          <span className="terminal-dot" aria-hidden="true" />
          <code>reposcope investigate --static</code>
          <dl>
            <div><dt>输入</dt><dd>公开 Python 仓库 + Issue</dd></div>
            <div><dt>输出</dt><dd>带不可变引用的调查报告</dd></div>
            <div><dt>边界</dt><dd>只读静态分析</dd></div>
          </dl>
        </aside>
      </section>

      <section className="entry-grid page-shell" aria-label="选择调查方式">
        <article className="entry-card live-card">
          <div className="entry-number">01</div>
          <p className="eyebrow">LOCAL MODE</p>
          <h2>本地实时分析</h2>
          <p>连接你本机的 RepoScope API，观察安全的公开事件并审阅报告。</p>
          <form onSubmit={submit} noValidate>
            <div className="field-group">
              <label htmlFor="repo-url">GitHub 仓库 URL</label>
              <input
                id="repo-url"
                type="url"
                value={repoUrl}
                onChange={(event) => { setRepoUrl(event.target.value); intent.current = null; }}
                placeholder="https://github.com/owner/repository"
                aria-invalid={validation.repo !== undefined}
                aria-describedby={validation.repo ? "repo-error" : undefined}
              />
              {validation.repo && <span id="repo-error" className="field-error">{validation.repo}</span>}
            </div>
            <div className="field-group issue-field">
              <label htmlFor="issue-number">Issue 编号</label>
              <input
                id="issue-number"
                type="number"
                inputMode="numeric"
                min="1"
                step="1"
                value={issueNumber}
                onChange={(event) => { setIssueNumber(event.target.value); intent.current = null; }}
                placeholder="42"
                aria-invalid={validation.issue !== undefined}
                aria-describedby={validation.issue ? "issue-error" : undefined}
              />
              {validation.issue && <span id="issue-error" className="field-error">{validation.issue}</span>}
            </div>
            <label className="check-row">
              <input type="checkbox" checked={useIdempotency} onChange={(event) => { setUseIdempotency(event.target.checked); intent.current = null; }} />
              <span>生成幂等重试标识 <small>不写入 URL 或浏览器存储</small></span>
            </label>
            <button className="button button-primary button-wide" type="submit" disabled={create.isPending || !liveEnabled}>
              {!liveEnabled ? "公开构建未启用" : create.isPending ? "正在创建…" : "开始静态调查"}
            </button>
            {!liveEnabled && <p className="notice notice-neutral">公开静态构建已禁用实时网络路径；请使用本地 Compose 的 live 构建。</p>}
            {createError && <p role="alert" className="notice notice-error">{createError}</p>}
          </form>
        </article>

        <article className="entry-card demo-entry">
          <div className="entry-number">02</div>
          <p className="eyebrow">BUNDLED MODE · ZERO NETWORK</p>
          <h2>预生成演示</h2>
          <p>三组提交在前端包内的架构与产品流程占位演练。它们不是模型结果，也不是基准成绩。</p>
          <div className="demo-card-list">
            {bundledDemos.cases.map((demo) => (
              <a className="demo-card" href={demoHref(demo.case_id)} key={demo.case_id} aria-label={`打开${demo.title.replace(" · 产品流程演练", "")}预生成演示`}>
                <div>
                  <span className="demo-badge">预生成演示</span>
                  <strong>{demo.title}</strong>
                  <small>{new URL(demo.repo_url).pathname.slice(1)} · Issue #{demo.issue_number}</small>
                </div>
                <span aria-hidden="true">↗</span>
              </a>
            ))}
          </div>
        </article>
      </section>

      <section className="principles page-shell" aria-label="设计原则">
        <div><span>01</span><strong>引用先于结论</strong><p>主假设必须由不可变提交中的有效行号支撑。</p></div>
        <div><span>02</span><strong>未知保持未知</strong><p>证据不足时明确停下，不虚构根因或修复。</p></div>
        <div><span>03</span><strong>操作边界清晰</strong><p>只提供调查路径，不执行代码，也不改仓库。</p></div>
      </section>
    </main>
  );
}
