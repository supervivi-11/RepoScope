import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import App from "../App";
import type { ApiClient } from "../api/client";
import type { AnalysisResponse } from "../contracts";
import { validAnalysisResponse } from "../test/fixtures";

afterEach(() => {
  window.location.hash = "";
});

test("keeps a submitted feedback command locked while refreshed backend state is still REVIEW_READY", async () => {
  window.location.hash = `#analysis/${validAnalysisResponse.analysis_id}`;
  const submitFeedback = vi.fn<ApiClient["submitFeedback"]>().mockResolvedValue({
    analysis_id: validAnalysisResponse.analysis_id,
    status: "REVIEW_READY",
  });
  const apiClient: ApiClient = {
    baseUrl: "",
    createAnalysis: vi.fn(),
    getAnalysis: vi.fn().mockResolvedValue(validAnalysisResponse),
    submitFeedback,
  };
  const streamFetch = vi.fn<typeof fetch>((_input, init) => new Promise((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
  }));
  render(<App mode="live" apiClient={apiClient} streamFetch={streamFetch} />);

  fireEvent.click(await screen.findByRole("button", { name: "接受报告" }));
  fireEvent.click(screen.getByRole("button", { name: "确认接受" }));
  await waitFor(() => expect(submitFeedback).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.getByText("操作已提交，等待后台状态变化。")).toBeInTheDocument());

  expect(screen.getByRole("button", { name: "接受报告" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "接受报告" }));
  expect(submitFeedback).toHaveBeenCalledTimes(1);
});

test("unmounting after the event reader is active aborts and cancels the stream", async () => {
  window.location.hash = `#analysis/${validAnalysisResponse.analysis_id}`;
  let cancelled = false;
  let readerStarted!: () => void;
  const started = new Promise<void>((resolve) => { readerStarted = resolve; });
  const streamFetch = vi.fn<typeof fetch>().mockResolvedValue(new Response(new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(new TextEncoder().encode(": heartbeat\n")); },
    pull() { readerStarted(); },
    cancel() { cancelled = true; },
  }), { status: 200, headers: { "Content-Type": "text/event-stream" } }));
  const apiClient: ApiClient = {
    baseUrl: "",
    createAnalysis: vi.fn(),
    getAnalysis: vi.fn().mockResolvedValue({ ...validAnalysisResponse, status: "INVESTIGATING", current_report: null, report_history: [] }),
    submitFeedback: vi.fn(),
  };
  const rendered = render(<App mode="live" apiClient={apiClient} streamFetch={streamFetch} />);

  await act(async () => { await started; });
  act(() => rendered.unmount());
  await waitFor(() => expect(cancelled).toBe(true));
});

test("shows a fixed unavailable-service message without reflecting exception detail", async () => {
  window.location.hash = `#analysis/${validAnalysisResponse.analysis_id}`;
  const apiClient: ApiClient = {
    baseUrl: "",
    createAnalysis: vi.fn(),
    getAnalysis: vi.fn().mockRejectedValue(new Error("Authorization sk-never-render")),
    submitFeedback: vi.fn(),
  };

  render(<App mode="live" apiClient={apiClient} streamFetch={vi.fn()} />);

  expect(await screen.findByRole("heading", { name: "调查暂不可用" }, { timeout: 3_000 })).toBeInTheDocument();
  expect(document.body.textContent).not.toContain("sk-never-render");
});

test("historical SSE status never overwrites the authoritative GET snapshot", async () => {
  window.location.hash = `#analysis/${validAnalysisResponse.analysis_id}`;
  let releaseSecond!: () => void;
  const secondRead = new Promise<AnalysisResponse>((resolve) => {
    releaseSecond = () => resolve({ ...validAnalysisResponse, status: "QUEUED", current_report: null, report_history: [] });
  });
  const apiClient: ApiClient = {
    baseUrl: "",
    createAnalysis: vi.fn(),
    getAnalysis: vi.fn()
      .mockResolvedValueOnce({ ...validAnalysisResponse, status: "QUEUED", current_report: null, report_history: [] })
      .mockReturnValueOnce(secondRead),
    submitFeedback: vi.fn(),
  };
  const streamFetch = vi.fn<typeof fetch>().mockResolvedValue(new Response(
    'id: 1\nevent: report_accepted\ndata: {"status":"COMPLETED","tool_calls":12}\n\n',
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  ));
  render(<App mode="live" apiClient={apiClient} streamFetch={streamFetch} />);

  await screen.findByRole("heading", { name: "调查工作区" });
  await screen.findByText("报告已接受 · 已完成");
  expect(document.querySelector(".status-pill")).toHaveTextContent("排队");
  expect(screen.queryByRole("heading", { name: "调查报告" })).not.toBeInTheDocument();
  act(() => releaseSecond());
});
