import { describe, expect, test, vi } from "vitest";

import { validAnalysisResponse } from "../test/fixtures";

const encoder = new TextEncoder();

function streamed(...chunks: string[]): Response {
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
}

describe("fetch-stream SSE", () => {
  test("parses events split across chunks and ignores duplicate or out-of-order IDs", async () => {
    const { consumeSseResponse } = await import("./sse");
    const events: number[] = [];
    const response = streamed(
      "id: 2\nevent: indexing_started\nda",
      'ta: {"status":"INDEXING"}\n\nid: 1\nevent: analysis_queued\ndata: {"status":"QUEUED"}\n\n',
      'id: 2\nevent: indexing_started\ndata: {"status":"INDEXING"}\n\n',
      'id: 3\nevent: report_accepted\ndata: {"status":"COMPLETED"}\n\n',
    );

    const result = await consumeSseResponse(response, 1, (event) => events.push(event.id));

    expect(events).toEqual([2, 3]);
    expect(result).toEqual({ lastEventId: 3, terminal: true });
  });

  test("reconnects with Last-Event-ID using bounded exponential backoff", async () => {
    const { watchAnalysisEvents } = await import("./sse");
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        streamed('id: 1\nevent: ingestion_started\ndata: {"status":"INGESTING"}\n\n'),
      )
      .mockResolvedValueOnce(
        streamed(
          'id: 1\nevent: ingestion_started\ndata: {"status":"INGESTING"}\n\n',
          'id: 2\nevent: investigation_started\ndata: {"status":"INVESTIGATING"}\n\n',
        ),
      )
      .mockResolvedValueOnce(
        streamed('id: 3\nevent: report_accepted\ndata: {"status":"COMPLETED"}\n\n'),
      );
    const delays: number[] = [];
    const received: number[] = [];

    await watchAnalysisEvents({
      analysisId: validAnalysisResponse.analysis_id,
      baseUrl: "https://local.example",
      fetch: fetcher,
      getAnalysis: vi.fn(),
      signal: new AbortController().signal,
      onEvent: (event) => received.push(event.id),
      onSnapshot: vi.fn(),
      onConnection: vi.fn(),
      sleep: async (delay) => {
        delays.push(delay);
      },
      maxReconnectAttempts: 3,
    });

    expect(received).toEqual([1, 2, 3]);
    expect(delays).toEqual([250, 500]);
    expect(new Headers(fetcher.mock.calls[0][1]?.headers).get("Last-Event-ID")).toBeNull();
    expect(new Headers(fetcher.mock.calls[1][1]?.headers).get("Last-Event-ID")).toBe("1");
    expect(new Headers(fetcher.mock.calls[2][1]?.headers).get("Last-Event-ID")).toBe("2");
  });

  test("falls back to periodic GET polling after the reconnect budget", async () => {
    const { watchAnalysisEvents } = await import("./sse");
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new TypeError("stream unavailable"));
    const getAnalysis = vi
      .fn()
      .mockResolvedValueOnce({ ...validAnalysisResponse, status: "INVESTIGATING" })
      .mockResolvedValueOnce({ ...validAnalysisResponse, status: "COMPLETED" });
    const connectionStates: string[] = [];
    const snapshots: string[] = [];
    const delays: number[] = [];

    await watchAnalysisEvents({
      analysisId: validAnalysisResponse.analysis_id,
      baseUrl: "",
      fetch: fetcher,
      getAnalysis,
      signal: new AbortController().signal,
      onEvent: vi.fn(),
      onSnapshot: (snapshot) => snapshots.push(snapshot.status),
      onConnection: (state) => connectionStates.push(state),
      sleep: async (delay) => {
        delays.push(delay);
      },
      maxReconnectAttempts: 2,
      pollIntervalMs: 5_000,
    });

    expect(fetcher).toHaveBeenCalledTimes(3);
    expect(connectionStates).toContain("polling");
    expect(snapshots).toEqual(["INVESTIGATING", "COMPLETED"]);
    expect(delays).toEqual([250, 500, 5_000]);
  });

  test("aborting an active route cancels the fetch and never starts polling", async () => {
    const { watchAnalysisEvents } = await import("./sse");
    const controller = new AbortController();
    const getAnalysis = vi.fn();
    const fetcher = vi.fn<typeof fetch>((_input, init) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
      }),
    );

    const watching = watchAnalysisEvents({
      analysisId: validAnalysisResponse.analysis_id,
      baseUrl: "",
      fetch: fetcher,
      getAnalysis,
      signal: controller.signal,
      onEvent: vi.fn(),
      onSnapshot: vi.fn(),
      onConnection: vi.fn(),
    });
    controller.abort();
    await watching;

    expect(fetcher.mock.calls[0][1]?.signal).toBe(controller.signal);
    expect(getAnalysis).not.toHaveBeenCalled();
  });

  test("rejects an oversized unterminated frame without retaining an unbounded buffer", async () => {
    const { consumeSseResponse, SseProtocolError } = await import("./sse");
    let cancelled = false;
    const response = new Response(new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array(70_000).fill(97));
      },
      cancel() { cancelled = true; },
    }), { status: 200, headers: { "Content-Type": "text/event-stream" } });

    await expect(consumeSseResponse(response, 0, vi.fn())).rejects.toBeInstanceOf(SseProtocolError);
    expect(cancelled).toBe(true);
  });

  test("an abort after the response reader is active cancels that reader", async () => {
    const { consumeSseResponse } = await import("./sse");
    const controller = new AbortController();
    let cancelled = false;
    let started!: () => void;
    const readerStarted = new Promise<void>((resolve) => { started = resolve; });
    const response = new Response(new ReadableStream<Uint8Array>({
      pull() { started(); },
      cancel() { cancelled = true; },
    }), { status: 200, headers: { "Content-Type": "text/event-stream" } });

    const consuming = consumeSseResponse(response, 0, vi.fn(), controller.signal);
    await readerStarted;
    controller.abort();
    await expect(consuming).rejects.toMatchObject({ name: "AbortError" });
    expect(cancelled).toBe(true);
  });

  test("canonicalizes secret-bearing safe errors and unknown public event names", async () => {
    const { consumeSseResponse } = await import("./sse");
    const events: import("../contracts").PublicEvent[] = [];
    await consumeSseResponse(streamed(
      'id: 1\nevent: mystery_event\ndata: {"safe_error":"Authorization: Bearer sk-super-secret"}\n\n',
      'id: 2\nevent: analysis_failed\ndata: {"status":"FAILED"}\n\n',
    ), 0, (event) => events.push(event));

    expect(events[0].data.safe_error).toBe("公开步骤未能安全完成。");
    expect(JSON.stringify(events)).not.toContain("sk-super-secret");
  });

  test("rejects malformed SSE JSON as a protocol error without emitting a partial event", async () => {
    const { consumeSseResponse, SseProtocolError } = await import("./sse");
    const onEvent = vi.fn();

    await expect(consumeSseResponse(
      streamed("id: 1\nevent: investigation_started\ndata: {not-json}\n\n"),
      0,
      onEvent,
    )).rejects.toBeInstanceOf(SseProtocolError);
    expect(onEvent).not.toHaveBeenCalled();
  });
});
