import { KNOWN_ANALYSIS_STATUSES, isKnownAnalysisStatus } from "../contracts";

const STATUS_LABELS: Record<(typeof KNOWN_ANALYSIS_STATUSES)[number], string> = {
  QUEUED: "排队",
  INGESTING: "获取仓库",
  INDEXING: "建立索引",
  INVESTIGATING: "静态调查",
  REVIEW_READY: "等待确认",
  REVISING: "修订中",
  COMPLETED: "已完成",
  FAILED: "失败",
};

export function statusLabel(status: string): string {
  return isKnownAnalysisStatus(status) ? STATUS_LABELS[status] : `未知状态：${status}`;
}

export function StatusTimeline({ currentStatus, observedStatuses = [] }: { currentStatus: string; observedStatuses?: string[] }) {
  const observed = new Set([...observedStatuses, currentStatus]);
  return (
    <section className="panel status-panel" aria-labelledby="status-heading">
      <div className="section-heading-row">
        <div>
          <p className="eyebrow">ANALYSIS PIPELINE</p>
          <h2 id="status-heading">调查进度</h2>
        </div>
        <span className="status-pill" data-status={currentStatus}>
          {statusLabel(currentStatus)}
        </span>
      </div>
      <ol className="status-timeline" aria-label="分析状态时间线">
        {KNOWN_ANALYSIS_STATUSES.map((status) => {
          const isCurrent = status === currentStatus;
          const wasObserved = observed.has(status);
          return (
            <li key={status} className={isCurrent ? "is-current" : wasObserved ? "is-observed" : "is-unobserved"}>
              <span className="status-marker" aria-hidden="true" />
              <span className="status-name">{STATUS_LABELS[status]}</span>
              <code>{status}</code>
              {isCurrent && <span className="sr-status">当前状态</span>}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
