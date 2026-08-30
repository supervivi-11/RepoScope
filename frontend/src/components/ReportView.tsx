import { useState } from "react";

import type { AnalysisReport, EvidenceCitation } from "../contracts";

interface ReportViewProps {
  report: AnalysisReport;
  repoUrl: string;
}

function confidenceText(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function buildGithubPermalink(repoUrl: string, citation: EvidenceCitation): string | null {
  if (!/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repoUrl)) return null;
  if (!/^[0-9a-f]{40}$/.test(citation.commit_sha)) return null;
  if (
    citation.path.startsWith("/") ||
    citation.path.includes("\\") ||
    citation.path.split("/").some((part) => part === "" || part === "." || part === "..")
  ) return null;
  const encodedPath = citation.path.split("/").map(encodeURIComponent).join("/");
  return `${repoUrl}/blob/${citation.commit_sha}/${encodedPath}#L${citation.start_line}-L${citation.end_line}`;
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }
  return (
    <button className="copy-button" type="button" aria-label={label} onClick={() => void copy()}>
      {copied ? "已复制" : "复制"}
    </button>
  );
}

function Empty({ children }: { children: string }) {
  return <p className="empty-state">{children}</p>;
}

export function ReportView({ report, repoUrl }: ReportViewProps) {
  return (
    <article className="report-stack" aria-labelledby="report-heading">
      <header className="panel report-header">
        <div>
          <p className="eyebrow">EVIDENCE-BACKED REPORT</p>
          <h2 id="report-heading">调查报告</h2>
        </div>
        <div className="report-outcome">
          <strong>{report.outcome === "root_cause_identified" ? "根因候选已识别" : "证据不足"}</strong>
          <span>整体置信度 {confidenceText(report.confidence)}</span>
        </div>
      </header>

      <section className="panel report-section">
        <h3>Issue 摘要</h3>
        <p>{report.issue_summary}</p>
        <div className="behavior-grid">
          <div><h4>观察到的行为</h4><p>{report.observed_behavior}</p></div>
          <div><h4>预期行为</h4><p>{report.expected_behavior}</p></div>
        </div>
      </section>

      <section className="panel report-section">
        <h3>主要假设</h3>
        {report.primary_hypothesis ? (
          <div className="hypothesis-card primary-hypothesis">
            <span className="confidence">置信度 {confidenceText(report.primary_hypothesis.confidence)}</span>
            <p>{report.primary_hypothesis.statement}</p>
          </div>
        ) : <Empty>没有足够证据形成主要假设。</Empty>}
        <h4>替代假设</h4>
        {report.alternative_hypotheses.length > 0 ? (
          <ul className="card-list">
            {report.alternative_hypotheses.map((hypothesis, index) => (
              <li key={`${hypothesis.statement}-${index}`}>
                <span className="confidence">{confidenceText(hypothesis.confidence)}</span>
                <p>{hypothesis.statement}</p>
              </li>
            ))}
          </ul>
        ) : <Empty>没有保留替代假设。</Empty>}
      </section>

      <section className="panel report-section">
        <h3>证据</h3>
        {report.evidence.length > 0 ? (
          <div className="evidence-list">
            {report.evidence.map((citation, index) => {
              const permalink = buildGithubPermalink(repoUrl, citation);
              return (
                <article className="evidence-card" key={`${citation.path}-${citation.start_line}-${index}`}>
                  <header>
                    <div>
                      <code>{citation.path}:{citation.start_line}-{citation.end_line}</code>
                      <span className="commit">@ {citation.commit_sha.slice(0, 10)}</span>
                    </div>
                    <CopyButton value={citation.path} label={`复制路径 ${citation.path}`} />
                  </header>
                  <p>{citation.explanation}</p>
                  {citation.excerpt.length > 0 && (
                    <pre aria-label={`${citation.path} 代码摘录`}>
                      <code>{citation.excerpt.split("\n").map((line, lineIndex) => <span key={lineIndex}>{line}</span>)}</code>
                    </pre>
                  )}
                  {permalink && (
                    <a
                      className="text-link"
                      href={permalink}
                      target="_blank"
                      rel="noreferrer"
                      aria-label={`在 GitHub 查看 ${citation.path} 第 ${citation.start_line} 到 ${citation.end_line} 行`}
                    >
                      在不可变提交中查看 ↗
                    </a>
                  )}
                </article>
              );
            })}
          </div>
        ) : <Empty>当前没有通过不可变快照校验的代码证据。</Empty>}
      </section>

      <div className="report-grid">
        <section className="panel report-section">
          <h3>受影响文件</h3>
          {report.impacted_files.length > 0 ? (
            <ul className="file-list">
              {report.impacted_files.map((file) => (
                <li key={file.path}>
                  <div className="file-heading"><code>{file.path}</code><CopyButton value={file.path} label={`复制路径 ${file.path}`} /></div>
                  <p>{file.explanation}</p>
                  <small>置信参考：报告整体 {confidenceText(report.confidence)}</small>
                </li>
              ))}
            </ul>
          ) : <Empty>暂不建议修改文件。</Empty>}
        </section>
        <section className="panel report-section">
          <h3>实施步骤</h3>
          {report.implementation_steps.length > 0 ? (
            <ol className="numbered-list">{report.implementation_steps.map((step, index) => <li key={`${step}-${index}`}>{step}</li>)}</ol>
          ) : <Empty>证据不足，暂不生成实施步骤。</Empty>}
        </section>
        <section className="panel report-section">
          <h3>建议测试</h3>
          {report.proposed_tests.length > 0 ? (
            <ul className="card-list">{report.proposed_tests.map((test) => <li key={test.name}><strong>{test.name}</strong><p>{test.description}</p></li>)}</ul>
          ) : <Empty>证据不足，暂不提出测试。</Empty>}
        </section>
        <section className="panel report-section">
          <h3>不确定项</h3>
          {report.uncertainties.length > 0 ? (
            <ul className="uncertainty-list">{report.uncertainties.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul>
          ) : <Empty>报告没有记录额外不确定项。</Empty>}
        </section>
      </div>
    </article>
  );
}
