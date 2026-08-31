import { describe, expect, test, vi } from "vitest";

import { validAnalysisResponse, validReport } from "../test/fixtures";

describe("RepoScope API client", () => {
  test("accepts both new and idempotently replayed create responses without putting keys in URLs", async () => {
    const { createApiClient } = await import("./client");
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ analysis_id: validAnalysisResponse.analysis_id, status: "QUEUED" }), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ analysis_id: validAnalysisResponse.analysis_id, status: "QUEUED" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    const client = createApiClient({ baseUrl: "https://local.example/base/", fetch: fetcher });

    const first = await client.createAnalysis(
      { repo_url: "https://github.com/acme/parser", issue_number: 42 },
      "retry-42",
    );
    const replay = await client.createAnalysis(
      { repo_url: "https://github.com/acme/parser", issue_number: 42 },
      "retry-42",
    );

    expect(first).toEqual(replay);
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toBe("https://local.example/base/api/v1/analyses");
    expect(String(url)).not.toContain("retry-42");
    expect(new Headers(init?.headers).get("Idempotency-Key")).toBe("retry-42");
  });

  test("encodes analysis identities and runtime-decodes get and feedback responses", async () => {
    const { createApiClient } = await import("./client");
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(validAnalysisResponse), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ analysis_id: validAnalysisResponse.analysis_id, status: "REVISING" }), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        }),
      );
    const client = createApiClient({ baseUrl: "", fetch: fetcher });

    await client.getAnalysis(validAnalysisResponse.analysis_id);
    const feedback = await client.submitFeedback(validAnalysisResponse.analysis_id, {
      action: "revise",
      comment: "Check the boundary guard.",
    });

    expect(fetcher.mock.calls[0][0]).toBe(`/api/v1/analyses/${validAnalysisResponse.analysis_id}`);
    expect(fetcher.mock.calls[1][0]).toContain(`${validAnalysisResponse.analysis_id}/feedback`);
    expect(feedback.status).toBe("REVISING");
  });

  test("accepts a schema-valid completed report above the old 1 MiB response cap", async () => {
    const { createApiClient } = await import("./client");
    const evidence = Array.from({ length: 23 }, (_, index) => ({
      ...validReport.evidence[0],
      path: `src/large-${index}.py`,
      start_line: 1,
      end_line: 1,
      excerpt: `${index}\n${"x".repeat(49_990)}`.slice(0, 50_000),
      explanation: "Large but contract-valid evidence excerpt.",
    }));
    const report = {
      ...validReport,
      evidence,
      primary_hypothesis: validReport.primary_hypothesis === null
        ? null
        : { ...validReport.primary_hypothesis, evidence: evidence.slice(0, 1) },
    };
    const completedResponse = {
      ...validAnalysisResponse,
      status: "COMPLETED",
      current_report: report,
      report_history: [
        { version: 1, report, created_at: "2026-08-30T08:02:00Z" },
        { version: 2, report, created_at: "2026-08-30T08:03:00Z" },
      ],
    };
    const body = JSON.stringify(completedResponse);
    const client = createApiClient({
      fetch: vi.fn<typeof fetch>().mockResolvedValue(
        new Response(body, {
          status: 200,
          headers: { "Content-Type": "application/json", "Content-Length": String(body.length) },
        }),
      ),
    });

    expect(new TextEncoder().encode(body).byteLength).toBeGreaterThan(1024 * 1024);
    const result = await client.getAnalysis(validAnalysisResponse.analysis_id);

    expect(result.status).toBe("COMPLETED");
    expect(result.current_report?.evidence).toHaveLength(evidence.length);
    expect(result.report_history).toHaveLength(2);
  });

  test("turns incompatible or secret-bearing failures into fixed safe UI errors", async () => {
    const { ApiError, createApiClient } = await import("./client");
    const malformed = createApiClient({
      fetch: vi.fn<typeof fetch>().mockResolvedValue(
        new Response('{"analysis_id":"sk-secret"', {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    });

    await expect(malformed.getAnalysis(validAnalysisResponse.analysis_id)).rejects.toMatchObject({
      name: "ApiError",
      code: "invalid_response",
      message: "服务器返回了无法安全读取的数据。",
    });

    const conflict = createApiClient({
      fetch: vi.fn<typeof fetch>().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: { code: "conflict", message: "Authorization sk-do-not-render" },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        ),
      ),
    });
    const caught = await conflict
      .submitFeedback(validAnalysisResponse.analysis_id, { action: "accept" })
      .catch((error: unknown) => error);

    expect(caught).toBeInstanceOf(ApiError);
    expect(caught).toMatchObject({ code: "conflict", message: "当前分析状态与此操作冲突，请刷新后重试。" });
    expect(String(caught)).not.toContain("sk-do-not-render");
  });

  test.each([
    "https://user:secret@api.example.test",
    "https://api.example.test/path?token=secret",
    "https://api.example.test/path#fragment",
    "javascript:alert(1)",
    "//evil.example.test",
    "https://api.example.test/path with space",
  ])("rejects an unsafe API base without reflecting it: %s", async (unsafeBase) => {
    const { ApiError, createApiClient } = await import("./client");
    let caught: unknown;
    try {
      createApiClient({ baseUrl: unsafeBase, fetch: vi.fn() });
    } catch (error) {
      caught = error;
    }
    expect(caught).toBeInstanceOf(ApiError);
    expect(caught).toMatchObject({ code: "unsafe_configuration", message: "本地 API 地址配置不安全。" });
    expect(String(caught)).not.toContain(unsafeBase);
  });

  test("accepts same-origin relative and credential-free HTTP(S) API bases", async () => {
    const { createApiClient } = await import("./client");
    expect(createApiClient({ baseUrl: "/reposcope/api/" }).baseUrl).toBe("/reposcope/api");
    expect(createApiClient({ baseUrl: "http://127.0.0.1:8000/v1/" }).baseUrl).toBe("http://127.0.0.1:8000/v1");
  });

  test("rejects GET and feedback payloads whose identity differs from the requested analysis", async () => {
    const { createApiClient } = await import("./client");
    const otherId = "6d68b2c0-b7bc-4d5b-a30b-d2ee54c533d2";
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ ...validAnalysisResponse, analysis_id: otherId }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ analysis_id: otherId, status: "REVIEW_READY" }), { status: 202 }));
    const client = createApiClient({ fetch: fetcher });

    await expect(client.getAnalysis(validAnalysisResponse.analysis_id)).rejects.toMatchObject({ code: "invalid_response" });
    await expect(client.submitFeedback(validAnalysisResponse.analysis_id, { action: "accept" })).rejects.toMatchObject({ code: "invalid_response" });
  });

  test("rejects analysis JSON whose declared size exceeds the derived schema bound", async () => {
    const { ANALYSIS_JSON_RESPONSE_BYTE_LIMIT, createApiClient } = await import("./client");
    const client = createApiClient({
      fetch: vi.fn().mockResolvedValue(
        new Response(JSON.stringify(validAnalysisResponse), {
          status: 200,
          headers: { "Content-Length": String(ANALYSIS_JSON_RESPONSE_BYTE_LIMIT + 1) },
        }),
      ),
    });

    await expect(client.getAnalysis(validAnalysisResponse.analysis_id)).rejects.toMatchObject({ code: "invalid_response" });
  });
});
