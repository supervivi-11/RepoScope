import { useState } from "react";

import { ApiError, type FeedbackInput } from "../api/client";

interface FeedbackControlsProps {
  revisionUsed: boolean;
  onSubmit: (input: FeedbackInput) => Promise<void>;
  commandPending?: boolean;
}

export function FeedbackControls({ revisionUsed, onSubmit, commandPending = false }: FeedbackControlsProps) {
  const [comment, setComment] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(input: FeedbackInput) {
    setError(null);
    setPending(true);
    try {
      await onSubmit(input);
      setConfirming(false);
      if (input.action === "revise") setComment("");
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : "反馈未能安全提交，请刷新后重试。",
      );
    } finally {
      setPending(false);
    }
  }

  function requestRevision() {
    if (comment.trim().length === 0) {
      setError("请填写非空修订说明。");
      return;
    }
    void submit({ action: "revise", comment: comment.trim() });
  }

  return (
    <section className="panel feedback-panel" aria-labelledby="feedback-heading">
      <p className="eyebrow">HUMAN REVIEW</p>
      <h2 id="feedback-heading">确认调查结果</h2>
      <p className="muted">系统只生成调查报告，不会修改仓库、创建提交或打开 PR。</p>

      {revisionUsed ? (
        <p className="notice notice-neutral">一次修订机会已使用。</p>
      ) : (
        <div className="field-group">
          <label htmlFor="revision-comment">修订说明</label>
          <textarea
            id="revision-comment"
            value={comment}
            maxLength={4_000}
            disabled={pending || commandPending}
            onChange={(event) => setComment(event.target.value)}
            placeholder="指出需要补充核查的证据或假设（最多 4000 字）"
          />
          <div className="field-meta">
            <span>仅允许一次修订</span>
            <span>{comment.length}/4000</span>
          </div>
          <button className="button button-secondary" type="button" disabled={pending || commandPending} onClick={requestRevision}>
            请求修订
          </button>
        </div>
      )}

      <div className="accept-zone">
        {!confirming ? (
          <button className="button button-primary" type="button" disabled={pending || commandPending} onClick={() => setConfirming(true)}>
            接受报告
          </button>
        ) : (
          <div className="confirm-box">
            <p>确认后，本次分析将进入完成状态。</p>
            <div className="button-row">
              <button className="button button-primary" type="button" aria-label="确认接受" disabled={pending} onClick={() => void submit({ action: "accept" })}>
                {pending ? "提交中…" : "确认接受"}
              </button>
              <button className="button button-quiet" type="button" disabled={pending} onClick={() => setConfirming(false)}>
                取消
              </button>
            </div>
          </div>
        )}
      </div>
      {error && <p role="alert" className="notice notice-error">{error}</p>}
      {commandPending && <p role="status" className="notice notice-neutral">操作已提交，等待后台状态变化。</p>}
    </section>
  );
}
