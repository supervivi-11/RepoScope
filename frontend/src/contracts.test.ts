import { describe, expect, test } from "vitest";

import { validReport } from "./test/fixtures";

describe("Task 5 runtime contracts", () => {
  test("decodes a complete analysis response and preserves its report history", async () => {
    const { decodeAnalysisResponse } = await import("./contracts");
    const response = decodeAnalysisResponse({
      analysis_id: "7e89c1d1-c8cd-4e6c-b40c-e3ff65d644e3",
      repo_url: "https://github.com/acme/parser",
      issue_number: 42,
      status: "REVIEW_READY",
      progress: { phase: "review", commit_sha: validReport.evidence[0].commit_sha },
      counters: { tool_calls: 4, evidence_rounds: 2, model_attempts: 1 },
      created_at: "2026-08-30T08:00:00Z",
      updated_at: "2026-08-30T08:02:00Z",
      error: null,
      current_report: validReport,
      report_history: [
        { version: 1, report: validReport, created_at: "2026-08-30T08:02:00Z" },
      ],
    });

    expect(response.status).toBe("REVIEW_READY");
    expect(response.current_report?.evidence[0].path).toBe("src/parser.py");
    expect(response.report_history).toHaveLength(1);
  });

  test("rejects malformed external shapes instead of partially rendering them", async () => {
    const { ContractError, decodeAnalysisResponse } = await import("./contracts");

    expect(() =>
      decodeAnalysisResponse({
        analysis_id: "not-a-uuid",
        repo_url: "https://github.com/acme/parser",
        issue_number: 42,
        status: "REVIEW_READY",
        progress: {},
        counters: {},
        created_at: "not-a-date",
        updated_at: "2026-08-30T08:02:00Z",
        error: null,
        current_report: null,
        report_history: [],
      }),
    ).toThrow(ContractError);

    expect(() =>
      decodeAnalysisResponse({
        analysis_id: "7e89c1d1-c8cd-4e6c-b40c-e3ff65d644e3",
        repo_url: "https://github.com/acme/parser",
        issue_number: 42,
        status: "REVIEW_READY",
        progress: {},
        counters: {},
        created_at: "2026-08-30T08:00:00Z",
        updated_at: "2026-08-30T08:02:00Z",
        error: null,
        current_report: { ...validReport, confidence: 2 },
        report_history: [],
      }),
    ).toThrow("服务器返回了不兼容的数据");
  });

  test("keeps a syntactically safe future status distinguishable from known states", async () => {
    const { decodeAnalysisStatus, isKnownAnalysisStatus } = await import("./contracts");

    const futureStatus = decodeAnalysisStatus("PAUSED_FOR_REVIEW");

    expect(futureStatus).toBe("PAUSED_FOR_REVIEW");
    expect(isKnownAnalysisStatus(futureStatus)).toBe(false);
    expect(() => decodeAnalysisStatus("<script>alert(1)</script>")).toThrow();
  });

  test("decodes named public events while ignoring non-displayable private fields", async () => {
    const { decodePublicEvent } = await import("./contracts");

    const event = decodePublicEvent(7, "tool_succeeded", {
      phase: "investigating",
      status: "INVESTIGATING",
      tool_name: "read_code",
      tool_calls: 3,
      evidence_rounds: 1,
      model_attempts: 1,
      raw_prompt: "must never render",
    });

    expect(event).toEqual({
      id: 7,
      event_type: "tool_succeeded",
      data: {
        phase: "investigating",
        status: "INVESTIGATING",
        tool_name: "read_code",
        tool_calls: 3,
        evidence_rounds: 1,
        model_attempts: 1,
      },
    });
  });

  test("redacts credential patterns from public report strings before they can reach the DOM", async () => {
    const { decodeAnalysisReport } = await import("./contracts");
    const report = decodeAnalysisReport({
      ...validReport,
      issue_summary: "Authorization: Bearer sk-live-super-secret caused the failure",
      uncertainties: ["See https://user:password@example.test/private"],
    });

    expect(report.issue_summary).toBe("[已隐藏凭据] caused the failure");
    expect(report.uncertainties[0]).toBe("See https://[已隐藏凭据]@example.test/private");
    expect(JSON.stringify(report)).not.toContain("super-secret");
    expect(JSON.stringify(report)).not.toContain("password");
  });
});
