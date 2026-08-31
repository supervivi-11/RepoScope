import {
  ContractError,
  decodeAnalysisResponse,
  decodeCreateAnalysisResponse,
  decodeErrorResponse,
  decodeFeedbackResponse,
  type AnalysisResponse,
  type CreateAnalysisResponse,
  type FeedbackResponse,
} from "../contracts";
import { configuredApiBaseUrl as buildApiBaseUrl } from "../config";

export interface CreateAnalysisInput {
  repo_url: string;
  issue_number: number;
}

export type FeedbackInput =
  | { action: "accept" }
  | { action: "revise"; comment: string };

export interface ApiClient {
  readonly baseUrl: string;
  createAnalysis(input: CreateAnalysisInput, idempotencyKey?: string): Promise<CreateAnalysisResponse>;
  getAnalysis(analysisId: string): Promise<AnalysisResponse>;
  submitFeedback(analysisId: string, input: FeedbackInput): Promise<FeedbackResponse>;
}

export interface ApiClientOptions {
  baseUrl?: string;
  fetch?: typeof fetch;
}

export class ApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const SAFE_MESSAGES: Record<string, string> = {
  conflict: "当前分析状态与此操作冲突，请刷新后重试。",
  validation_error: "提交内容未通过校验，请检查后重试。",
  not_found: "未找到这次分析。",
  database_unavailable: "本地分析服务暂时不可用。",
  upstream_unavailable: "上游公共数据源暂时不可用。",
  request_too_large: "请求内容超出限制。",
};

function normalizeBaseUrl(value: string): string {
  const trimmed = value.trim();
  if (trimmed === "") return "";
  if (/[\u0000-\u0020\u007F]/.test(trimmed)) {
    throw new ApiError("unsafe_configuration", "本地 API 地址配置不安全。");
  }
  if (trimmed.startsWith("/")) {
    if (trimmed.startsWith("//") || trimmed.includes("?") || trimmed.includes("#") || trimmed.includes("\\")) {
      throw new ApiError("unsafe_configuration", "本地 API 地址配置不安全。");
    }
    return trimmed.replace(/\/+$/, "");
  }
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    throw new ApiError("unsafe_configuration", "本地 API 地址配置不安全。");
  }
  if (
    (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
    parsed.username !== "" || parsed.password !== "" || parsed.search !== "" || parsed.hash !== ""
  ) {
    throw new ApiError("unsafe_configuration", "本地 API 地址配置不安全。");
  }
  return `${parsed.origin}${parsed.pathname}`.replace(/\/+$/, "");
}

export function configuredApiBaseUrl(): string {
  return normalizeBaseUrl(buildApiBaseUrl());
}

function endpoint(baseUrl: string, path: string): string {
  return `${baseUrl}${path}`;
}

const MAX_JSON_RESPONSE_BYTES = 1024 * 1024;

async function boundedResponseText(response: Response): Promise<string> {
  const declared = response.headers.get("Content-Length");
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > MAX_JSON_RESPONSE_BYTES)) {
    throw new ApiError("invalid_response", "服务器返回了无法安全读取的数据。", response.status);
  }
  if (response.body === null) return "";
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let received = 0;
  let text = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) return text + decoder.decode();
      received += value.byteLength;
      if (received > MAX_JSON_RESPONSE_BYTES) {
        await reader.cancel();
        throw new ApiError("invalid_response", "服务器返回了无法安全读取的数据。", response.status);
      }
      text += decoder.decode(value, { stream: true });
    }
  } finally {
    reader.releaseLock();
  }
}

async function parseJson(response: Response): Promise<unknown> {
  try {
    const text = await boundedResponseText(response);
    return JSON.parse(text) as unknown;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError("invalid_response", "服务器返回了无法安全读取的数据。", response.status);
  }
}

async function safeFailure(response: Response): Promise<never> {
  const payload = await parseJson(response);
  try {
    const error = decodeErrorResponse(payload);
    throw new ApiError(
      error.code,
      SAFE_MESSAGES[error.code] ?? "请求未能安全完成，请稍后重试。",
      response.status,
    );
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError("invalid_response", "服务器返回了无法安全读取的数据。", response.status);
  }
}

async function request<T>(
  fetcher: typeof fetch,
  url: string,
  init: RequestInit,
  decode: (value: unknown) => T,
): Promise<T> {
  let response: Response;
  try {
    response = await fetcher(url, init);
  } catch {
    throw new ApiError("unavailable", "无法连接本地分析服务，请确认服务已启动。");
  }
  if (!response.ok) return safeFailure(response);
  const payload = await parseJson(response);
  try {
    return decode(payload);
  } catch (error) {
    if (error instanceof ContractError) {
      throw new ApiError("invalid_response", "服务器返回了无法安全读取的数据。", response.status);
    }
    throw error;
  }
}

export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  const baseUrl = normalizeBaseUrl(options.baseUrl ?? configuredApiBaseUrl());
  const fetcher = options.fetch ?? globalThis.fetch.bind(globalThis);

  return {
    baseUrl,
    async createAnalysis(input, idempotencyKey) {
      const headers = new Headers({ "Content-Type": "application/json", Accept: "application/json" });
      if (idempotencyKey !== undefined) headers.set("Idempotency-Key", idempotencyKey);
      return request(
        fetcher,
        endpoint(baseUrl, "/api/v1/analyses"),
        {
          method: "POST",
          headers,
          body: JSON.stringify(input),
          credentials: "same-origin",
        },
        decodeCreateAnalysisResponse,
      );
    },
    async getAnalysis(analysisId) {
      const result = await request(
        fetcher,
        endpoint(baseUrl, `/api/v1/analyses/${encodeURIComponent(analysisId)}`),
        { method: "GET", headers: { Accept: "application/json" }, credentials: "same-origin" },
        decodeAnalysisResponse,
      );
      if (result.analysis_id !== analysisId) throw new ApiError("invalid_response", "服务器返回了无法安全读取的数据。");
      return result;
    },
    async submitFeedback(analysisId, input) {
      const result = await request(
        fetcher,
        endpoint(baseUrl, `/api/v1/analyses/${encodeURIComponent(analysisId)}/feedback`),
        {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify(input),
          credentials: "same-origin",
        },
        decodeFeedbackResponse,
      );
      if (result.analysis_id !== analysisId) throw new ApiError("invalid_response", "服务器返回了无法安全读取的数据。");
      return result;
    },
  };
}
