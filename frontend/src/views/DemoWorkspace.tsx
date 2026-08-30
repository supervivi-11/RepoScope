import { useEffect, useReducer } from "react";

import { EventTimeline } from "../components/EventTimeline";
import { ReportView } from "../components/ReportView";
import { StatusTimeline } from "../components/StatusTimeline";
import type { PublicEvent } from "../contracts";
import { bundledDemos } from "../demo/artifacts";
import { createPlaybackState, demoPlaybackReducer } from "../demo/player";

export function DemoWorkspace({ caseId, intervalMs = 900 }: { caseId: string; intervalMs?: number }) {
  const demo = bundledDemos.cases.find((item) => item.case_id === caseId);
  const [playback, dispatch] = useReducer(demoPlaybackReducer, undefined, createPlaybackState);

  useEffect(() => {
    if (!demo || !playback.playing) return undefined;
    const timer = window.setInterval(() => {
      dispatch({ type: "tick", eventCount: demo.events.length });
    }, intervalMs);
    return () => window.clearInterval(timer);
  }, [demo, intervalMs, playback.playing]);

  if (!demo) {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell workspace-page">
        <section className="panel error-panel"><p className="eyebrow">NOT FOUND</p><h1>没有这个预生成演示</h1><a href="#">返回首页</a></section>
      </main>
    );
  }

  const visibleEvents: PublicEvent[] = demo.events.slice(0, playback.visibleEventCount).map((event) => ({
    id: event.sequence,
    event_type: event.event_type,
    data: event.data,
  }));
  const currentStatus = visibleEvents.at(-1)?.data.status ?? "QUEUED";

  return (
    <main id="main-content" tabIndex={-1} className="page-shell workspace-page">
      <header className="workspace-header demo-workspace-header">
        <div>
          <div className="badge-row"><span className="demo-badge">预生成演示</span><span>artifact v{demo.artifact_version}</span></div>
          <h1>{demo.title}</h1>
          <p>架构与产品流程占位演练，不是真实基准结果。</p>
        </div>
        <div className="workspace-identity">
          <code>{new URL(demo.repo_url).pathname.slice(1)}</code>
          <span>Issue #{demo.issue_number}</span>
        </div>
      </header>

      <section className="panel playback-panel" aria-labelledby="playback-heading">
        <div>
          <p className="eyebrow">DETERMINISTIC PLAYBACK</p>
          <h2 id="playback-heading">事件回放</h2>
          <p>事件 {playback.visibleEventCount}/{demo.events.length} · 报告将在事件全部回放后显示</p>
        </div>
        <div className="playback-controls">
          <button className="button button-primary" type="button" disabled={playback.reportVisible} onClick={() => dispatch({ type: playback.playing ? "pause" : "play" })}>
            {playback.playing ? "暂停" : "播放"}
          </button>
          <button className="button button-secondary" type="button" disabled={playback.playing || playback.reportVisible} onClick={() => dispatch({ type: "step", eventCount: demo.events.length })}>
            单步
          </button>
          <button className="button button-quiet" type="button" onClick={() => dispatch({ type: "restart" })}>
            重新开始
          </button>
        </div>
        <div className="playback-track" aria-hidden="true"><span style={{ width: `${(playback.visibleEventCount / demo.events.length) * 100}%` }} /></div>
      </section>

      <div className="workspace-grid">
        <StatusTimeline currentStatus={currentStatus} observedStatuses={visibleEvents.flatMap((event) => event.data.status ? [event.data.status] : [])} />
        <EventTimeline events={visibleEvents} />
      </div>

      {playback.reportVisible ? (
        <ReportView report={demo.report} repoUrl={demo.repo_url} />
      ) : (
        <section className="panel report-locked" aria-live="polite">
          <span aria-hidden="true">⌁</span>
          <div><h2>报告尚未揭示</h2><p>播放或单步查看所有公开事件后显示预生成报告。</p></div>
        </section>
      )}
    </main>
  );
}
