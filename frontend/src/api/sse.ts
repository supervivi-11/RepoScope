import {
  decodePublicEvent,
  type AnalysisResponse,
  type PublicEvent,
} from "../contracts";

export type ConnectionState = "connecting" | "connected" | "reconnecting" | "polling" | "closed";

export interface SseConsumeResult {
  lastEventId: number;
  terminal: boolean;
}

export interface WatchAnalysisOptions {
  analysisId: string;
  baseUrl: string;
  fetch: typeof fetch;
  getAnalysis: (analysisId: string) => Promise<AnalysisResponse>;
  signal: AbortSignal;
  onEvent: (event: PublicEvent) => void;
  onSnapshot: (snapshot: AnalysisResponse) => void;
  onConnection: (state: ConnectionState) => void;
  sleep?: (delayMs: number, signal: AbortSignal) => Promise<void>;
  maxReconnectAttempts?: number;
  baseDelayMs?: number;
  maxDelayMs?: number;
  pollIntervalMs?: number;
}

export class SseProtocolError extends Error {
  constructor(message = "事件流不可用") {
    super(message);
    this.name = "SseProtocolError";
  }
}

const MAX_SSE_BUFFER_BYTES = 64 * 1024;

function terminalStatus(status: string | undefined): boolean {
  return status === "COMPLETED" || status === "FAILED";
}

function nextFrame(buffer: string): { frame: string; rest: string } | null {
  const match = /\r?\n\r?\n/.exec(buffer);
  if (match?.index === undefined) return null;
  return {
    frame: buffer.slice(0, match.index),
    rest: buffer.slice(match.index + match[0].length),
  };
}

function parseFrame(frame: string, lastEventId: number): PublicEvent | null {
  let id: string | undefined;
  let eventType: string | undefined;
  const data: string[] = [];
  for (const rawLine of frame.split(/\r?\n/)) {
    if (rawLine === "" || rawLine.startsWith(":")) continue;
    const separator = rawLine.indexOf(":");
    const field = separator === -1 ? rawLine : rawLine.slice(0, separator);
    let value = separator === -1 ? "" : rawLine.slice(separator + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "id") id = value;
    else if (field === "event") eventType = value;
    else if (field === "data") data.push(value);
  }
  if (id === undefined && eventType === undefined && data.length === 0) return null;
  if (id === undefined || eventType === undefined || !/^[0-9]+$/.test(id) || data.length === 0) {
    throw new SseProtocolError();
  }
  const numericId = Number(id);
  if (!Number.isSafeInteger(numericId) || numericId < 1) throw new SseProtocolError();
  if (numericId <= lastEventId) return null;
  let payload: unknown;
  try {
    payload = JSON.parse(data.join("\n")) as unknown;
  } catch {
    throw new SseProtocolError();
  }
  try {
    return decodePublicEvent(numericId, eventType, payload);
  } catch {
    throw new SseProtocolError();
  }
}

export async function consumeSseResponse(
  response: Response,
  afterEventId: number,
  onEvent: (event: PublicEvent) => void,
  signal?: AbortSignal,
): Promise<SseConsumeResult> {
  if (!response.ok || response.body === null) throw new SseProtocolError();
  const contentType = response.headers.get("Content-Type") ?? "";
  if (!contentType.toLowerCase().startsWith("text/event-stream")) throw new SseProtocolError();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let cursor = afterEventId;
  const abort = () => { void reader.cancel(); };
  signal?.addEventListener("abort", abort, { once: true });
  try {
    while (true) {
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      const { done, value } = await reader.read();
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      buffer += decoder.decode(value, { stream: !done });
      let framed = nextFrame(buffer);
      while (framed !== null) {
        buffer = framed.rest;
        if (new TextEncoder().encode(framed.frame).byteLength > MAX_SSE_BUFFER_BYTES) {
          throw new SseProtocolError("事件帧超出安全限制");
        }
        const event = parseFrame(framed.frame, cursor);
        if (event !== null) {
          cursor = event.id;
          onEvent(event);
          if (terminalStatus(event.data.status)) {
            await reader.cancel();
            return { lastEventId: cursor, terminal: true };
          }
        }
        framed = nextFrame(buffer);
      }
      if (new TextEncoder().encode(buffer).byteLength > MAX_SSE_BUFFER_BYTES) {
        throw new SseProtocolError("事件帧超出安全限制");
      }
      if (done) return { lastEventId: cursor, terminal: false };
    }
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  } finally {
    signal?.removeEventListener("abort", abort);
    reader.releaseLock();
  }
}

export function sleepWithAbort(delayMs: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException("Aborted", "AbortError"));
      return;
    }
    const finish = () => { signal.removeEventListener("abort", abort); resolve(); };
    const abort = () => {
      globalThis.clearTimeout(timeout);
      signal.removeEventListener("abort", abort);
      reject(new DOMException("Aborted", "AbortError"));
    };
    const timeout = globalThis.setTimeout(finish, delayMs);
    signal.addEventListener("abort", abort, { once: true });
  });
}

function isAbort(error: unknown, signal: AbortSignal): boolean {
  return signal.aborted || (error instanceof DOMException && error.name === "AbortError");
}

export async function watchAnalysisEvents(options: WatchAnalysisOptions): Promise<void> {
  const sleep = options.sleep ?? sleepWithAbort;
  const reconnectBudget = options.maxReconnectAttempts ?? 3;
  const baseDelay = options.baseDelayMs ?? 250;
  const maxDelay = options.maxDelayMs ?? 2_000;
  const pollInterval = options.pollIntervalMs ?? 5_000;
  const baseUrl = options.baseUrl.trim().replace(/\/+$/, "");
  let lastEventId = 0;
  let failures = 0;
  options.onConnection("connecting");

  while (!options.signal.aborted) {
    try {
      const headers = new Headers({ Accept: "text/event-stream", "Cache-Control": "no-cache" });
      if (lastEventId > 0) headers.set("Last-Event-ID", String(lastEventId));
      const response = await options.fetch(
        `${baseUrl}/api/v1/analyses/${encodeURIComponent(options.analysisId)}/events`,
        { method: "GET", headers, credentials: "same-origin", signal: options.signal },
      );
      options.onConnection("connected");
      const consumed = await consumeSseResponse(response, lastEventId, options.onEvent, options.signal);
      lastEventId = consumed.lastEventId;
      if (consumed.terminal) {
        const snapshot = await options.getAnalysis(options.analysisId);
        options.onSnapshot(snapshot);
        if (terminalStatus(snapshot.status)) {
          options.onConnection("closed");
          return;
        }
        throw new SseProtocolError("终态事件尚未完成权威发布");
      }
      throw new SseProtocolError("事件流已断开");
    } catch (error) {
      if (isAbort(error, options.signal)) {
        options.onConnection("closed");
        return;
      }
      failures += 1;
      if (failures <= reconnectBudget) {
        options.onConnection("reconnecting");
        const delay = Math.min(maxDelay, baseDelay * 2 ** (failures - 1));
        try {
          await sleep(delay, options.signal);
        } catch (sleepError) {
          if (isAbort(sleepError, options.signal)) {
            options.onConnection("closed");
            return;
          }
          throw sleepError;
        }
        continue;
      }
      break;
    }
  }

  if (options.signal.aborted) {
    options.onConnection("closed");
    return;
  }
  options.onConnection("polling");
  while (!options.signal.aborted) {
    try {
      const snapshot = await options.getAnalysis(options.analysisId);
      options.onSnapshot(snapshot);
      if (terminalStatus(snapshot.status)) {
        options.onConnection("closed");
        return;
      }
      await sleep(pollInterval, options.signal);
    } catch (error) {
      if (isAbort(error, options.signal)) {
        options.onConnection("closed");
        return;
      }
      try {
        await sleep(pollInterval, options.signal);
      } catch (sleepError) {
        if (isAbort(sleepError, options.signal)) {
          options.onConnection("closed");
          return;
        }
        throw sleepError;
      }
    }
  }
  options.onConnection("closed");
}
