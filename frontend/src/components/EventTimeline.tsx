import type { ConnectionState } from "../api/sse";
import type { PublicEvent } from "../contracts";
import { statusLabel } from "./StatusTimeline";

const EVENT_LABELS: Record<string, string> = {
  analysis_queued: "分析已排队",
  ingestion_started: "开始获取不可变仓库快照",
  indexing_started: "开始建立 Python 静态索引",
  investigation_started: "开始只读调查",
  issue_understood: "Issue 行为已结构化",
  evidence_round_started: "开始收集证据",
  tool_succeeded: "只读工具返回结果",
  tool_failed: "只读工具未能完成",
  investigation_completed: "证据调查完成",
  citation_validated: "代码引用已校验",
  citation_rejected: "无效代码引用已移除",
  report_composed: "调查报告已生成",
  review_ready: "报告等待确认",
  revision_requested: "已请求一次修订",
  report_revised: "修订报告已生成",
  report_accepted: "报告已接受",
  analysis_failed: "分析安全终止",
  model_failed: "模型调用未能安全完成",
  feedback_rejected: "反馈未被接受",
  revision_rejected: "修订请求未被接受",
  tool_budget_exhausted: "只读工具预算已用尽",
  round_budget_exhausted: "证据轮次预算已用尽",
};

const CONNECTION_LABELS: Record<ConnectionState, string> = {
  connecting: "正在连接实时事件",
  connected: "实时事件已连接",
  reconnecting: "事件流断开，正在重连",
  polling: "事件流不可用，已切换定时刷新",
  closed: "实时更新已结束",
};

export function connectionLabel(state: ConnectionState): string {
  return CONNECTION_LABELS[state];
}

function eventDescription(event: PublicEvent): string {
  const label = EVENT_LABELS[event.event_type] ?? "未知公开事件";
  if (event.data.tool_name) return `${label} · ${event.data.tool_name}`;
  if (event.data.status) return `${label} · ${statusLabel(event.data.status)}`;
  return label;
}

export function EventTimeline({ events, connection }: { events: PublicEvent[]; connection?: ConnectionState }) {
  return (
    <section className="panel event-panel" aria-labelledby="event-heading">
      <div className="section-heading-row">
        <div><p className="eyebrow">PUBLIC ACTIVITY</p><h2 id="event-heading">公开事件</h2></div>
        {connection && <span className={`connection connection-${connection}`}>{connectionLabel(connection)}</span>}
      </div>
      {events.length > 0 ? (
        <ol className="event-list">
          {events.map((event) => (
            <li key={event.id}>
              <span className="event-sequence">{String(event.id).padStart(2, "0")}</span>
              <div>
                <strong>{eventDescription(event)}</strong>
                {event.data.citation && <code>{event.data.citation.path}:{event.data.citation.start_line}-{event.data.citation.end_line}</code>}
                {event.data.safe_error && <p>{event.data.safe_error}</p>}
              </div>
            </li>
          ))}
        </ol>
      ) : <p className="empty-state">尚无公开工具或证据事件。</p>}
    </section>
  );
}
