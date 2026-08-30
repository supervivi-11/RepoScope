import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { ApiError, type ApiClient, type FeedbackInput } from "../api/client";
import { EventTimeline } from "../components/EventTimeline";
import { FeedbackControls } from "../components/FeedbackControls";
import { ReportView } from "../components/ReportView";
import { StatusTimeline } from "../components/StatusTimeline";
import type { AnalysisResponse, JsonObject, JsonValue, ReportVersion } from "../contracts";
import { analysisQueryKey, useAnalysisEvents } from "../hooks/useAnalysisEvents";

function repoName(repoUrl: string): string {
  try {
    return new URL(repoUrl).pathname.slice(1);
  } catch {
    return "未知仓库";
  }
}

function displayCounter(mapping: JsonObject, key: string): string {
  const value: JsonValue | undefined = mapping[key];
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? String(value) : "—";
}

function commitSha(analysis: AnalysisResponse): string | null {
  const progressCommit = analysis.progress.commit_sha;
  if (typeof progressCommit === "string" && /^[0-9a-f]{40}$/.test(progressCommit)) return progressCommit;
  return analysis.current_report?.evidence[0]?.commit_sha ?? null;
}

function safeAnalysisFailure(analysis: AnalysisResponse): string {
  if (!analysis.error) return "分析未能安全完成。";
  return `分析未能安全完成（${analysis.error.code}）。`;
}

function uniqueHistory(history: ReportVersion[]): ReportVersion[] {
  const byVersion = new Map<number, ReportVersion>();
  for (const item of history) byVersion.set(item.version, item);
  return [...byVersion.values()].sort((left, right) => left.version - right.version);
}

export function LiveWorkspace({
  analysisId,
  apiClient,
  streamFetch,
}: {
  analysisId: string;
  apiClient: ApiClient;
  streamFetch: typeof fetch;
}) {
  const queryClient = useQueryClient();
  const [submittedCommand, setSubmittedCommand] = useState<FeedbackInput["action"] | null>(null);
  const analysis = useQuery({
    queryKey: analysisQueryKey(analysisId),
    queryFn: () => apiClient.getAnalysis(analysisId),
    retry: 1,
  });
  const terminal = analysis.data?.status === "COMPLETED" || analysis.data?.status === "FAILED";
  const stream = useAnalysisEvents({
    analysisId,
    apiClient,
    streamFetch,
    enabled: analysis.data !== undefined && !terminal,
  });
  const feedback = useMutation({
    mutationFn: (input: FeedbackInput) => apiClient.submitFeedback(analysisId, input),
    onSuccess: (response) => {
      queryClient.setQueryData<AnalysisResponse>(analysisQueryKey(analysisId), (current) =>
        current ? { ...current, status: response.status } : current,
      );
      void queryClient.invalidateQueries({ queryKey: analysisQueryKey(analysisId) });
    },
  });

  useEffect(() => {
    if (analysis.data && analysis.data.status !== "REVIEW_READY") setSubmittedCommand(null);
  }, [analysis.data?.status]);

  if (analysis.isPending) {
    return <main id="main-content" tabIndex={-1} className="page-shell workspace-page"><section className="panel loading-panel" role="status"><span className="spinner" /><div><h1>正在载入调查</h1><p>读取公开分析状态与报告合约…</p></div></section></main>;
  }
  if (analysis.isError || !analysis.data) {
    const message = analysis.error instanceof ApiError
      ? analysis.error.message
      : "无法读取分析，请确认本地服务可用。";
    return (
      <main id="main-content" tabIndex={-1} className="page-shell workspace-page">
        <section className="panel error-panel"><p className="eyebrow">LOCAL SERVICE</p><h1>调查暂不可用</h1><p role="alert">{message}</p><button className="button button-secondary" type="button" onClick={() => void analysis.refetch()}>重新连接</button></section>
      </main>
    );
  }

  const data = analysis.data;
  const fixedCommit = commitSha(data);
  const history = uniqueHistory(data.report_history);
  const revisionUsed = history.length > 1 || displayCounter(data.counters, "user_revisions") === "1";

  async function submitFeedback(input: FeedbackInput): Promise<void> {
    if (submittedCommand !== null) return;
    setSubmittedCommand(input.action);
    try {
      await feedback.mutateAsync(input);
    } catch (error) {
      setSubmittedCommand(null);
      throw error;
    }
  }

  return (
    <main id="main-content" tabIndex={-1} className="page-shell workspace-page">
      <header className="workspace-header">
        <div>
          <div className="badge-row"><span className="live-badge">本地实时分析</span><span>Analysis {analysisId.slice(0, 8)}</span></div>
          <h1>调查工作区</h1>
          <p>只展示公开工具事件与经过合约校验的报告，不展示隐藏推理。</p>
        </div>
        <div className="workspace-identity"><code>{repoName(data.repo_url)}</code><span>Issue #{data.issue_number}</span></div>
      </header>

      <section className="metrics" aria-label="安全分析计数器">
        <div><span>固定提交</span><strong title={fixedCommit ?? "尚未解析"}>{fixedCommit ? fixedCommit.slice(0, 10) : "待解析"}</strong></div>
        <div><span>工具调用</span><strong>{displayCounter(data.counters, "tool_calls")}</strong><small>/ 12</small></div>
        <div><span>证据轮次</span><strong>{displayCounter(data.counters, "evidence_rounds")}</strong><small>/ 2</small></div>
        <div><span>模型尝试</span><strong>{displayCounter(data.counters, "model_attempts")}</strong></div>
        <div><span>Token</span><strong>{displayCounter(data.counters, "token_count")}</strong></div>
        <div><span>估算成本</span><strong>{displayCounter(data.counters, "estimated_cost_usd")}</strong></div>
      </section>

      <div className="workspace-grid">
        <StatusTimeline currentStatus={data.status} observedStatuses={stream.events.flatMap((event) => event.data.status ? [event.data.status] : [])} />
        <EventTimeline events={stream.events} connection={stream.connection} />
      </div>

      {data.status === "FAILED" && <section className="panel error-panel"><p className="eyebrow">SAFE FAILURE</p><h2>分析已安全终止</h2><p role="alert">{safeAnalysisFailure(data)}</p></section>}

      {data.current_report ? <ReportView report={data.current_report} repoUrl={data.repo_url} /> : (
        data.status !== "FAILED" && <section className="panel report-locked"><span aria-hidden="true">⌁</span><div><h2>等待调查报告</h2><p>当前阶段：{data.status}。报告通过引用校验后才会显示。</p></div></section>
      )}

      {history.length > 0 && (
        <section className="panel history-panel" aria-labelledby="history-heading">
          <div><p className="eyebrow">VERSION HISTORY</p><h2 id="history-heading">报告历史</h2></div>
          <ol>{history.map((item) => <li key={item.version}><strong>版本 {item.version}</strong><span>{new Date(item.created_at).toLocaleString("zh-CN")}</span><small>{item.report.outcome === "root_cause_identified" ? "根因候选" : "证据不足"}</small></li>)}</ol>
        </section>
      )}

      {data.status === "REVIEW_READY" && <FeedbackControls revisionUsed={revisionUsed} commandPending={submittedCommand !== null} onSubmit={submitFeedback} />}
    </main>
  );
}
