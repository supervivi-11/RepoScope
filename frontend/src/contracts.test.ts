import { describe, expect, test } from "vitest";

import { validReport } from "./test/fixtures";
import {
  ANALYSIS_RESPONSE_MAX_BYTES,
  EVIDENCE_EXCERPT_MAX_CHARS,
  HYPOTHESIS_EVIDENCE_REFS_MAX,
  REPORT_REQUIRED_TEXT_MAX_CHARS,
} from "./generated/limits";

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

  test.each(["\n", "\r\n", "\r"])(
    "preserves %j line delimiters while redacting report credentials",
    async (delimiter) => {
      const { decodeAnalysisReport } = await import("./contracts");
      const source = [
        "Authorization:",
        "Bearer sk-live-super-secret",
        'OPENAI_API_KEY="opaqueCredential123456"',
        '"password": "hunter2"',
        "DATABASE_PASSWORD=supersecretvalue",
        "AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP",
        'token = "abc def"',
        "token_count = 12",
        "AuthorizationPolicy = strict",
      ].join(delimiter);

      const report = decodeAnalysisReport({ ...validReport, issue_summary: source });

      expect(report.issue_summary.split(delimiter)).toHaveLength(9);
      expect(report.issue_summary.match(new RegExp(delimiter === "\r\n" ? "\\r\\n" : delimiter, "g"))).toHaveLength(8);
      expect(report.issue_summary).not.toContain("super-secret");
      expect(report.issue_summary).not.toContain("opaqueCredential123456");
      for (const secret of ["hunter2", "supersecretvalue", "AKIAABCDEFGHIJKLMNOP", "abc def"]) {
        expect(report.issue_summary).not.toContain(secret);
      }
      expect(report.issue_summary).toContain("token_count = 12");
      expect(report.issue_summary).toContain("AuthorizationPolicy = strict");
    },
  );

  test("enforces backend-generated report limits and compact hypothesis evidence refs", async () => {
    const { ContractError, decodeAnalysisReport, decodeAnalysisResponse } = await import("./contracts");
    const citationRef = {
      commit_sha: validReport.evidence[0].commit_sha,
      path: "src/parser.py",
      start_line: 14,
      end_line: 17,
    };
    const report = decodeAnalysisReport({
      ...validReport,
      issue_summary: "x".repeat(REPORT_REQUIRED_TEXT_MAX_CHARS),
      primary_hypothesis: {
        statement: "A compact evidence reference points at top-level evidence.",
        confidence: 0.75,
        evidence: Array.from({ length: HYPOTHESIS_EVIDENCE_REFS_MAX }, () => citationRef),
      },
      evidence: [
        {
          ...citationRef,
          excerpt: "😀".repeat(EVIDENCE_EXCERPT_MAX_CHARS),
          explanation: "Boundary-sized UTF-8 evidence excerpt.",
        },
      ],
    });

    expect(report.issue_summary).toHaveLength(REPORT_REQUIRED_TEXT_MAX_CHARS);
    expect(report.primary_hypothesis?.evidence[0]).toEqual(citationRef);
    expect("excerpt" in report.primary_hypothesis!.evidence[0]).toBe(false);

    expect(() => decodeAnalysisReport({ ...validReport, issue_summary: "x".repeat(REPORT_REQUIRED_TEXT_MAX_CHARS + 1) })).toThrow(ContractError);
    expect(() => decodeAnalysisReport({
      ...validReport,
      primary_hypothesis: {
        statement: "Full citations must not be nested under hypotheses.",
        confidence: 0.75,
        evidence: validReport.evidence,
      },
    })).toThrow(ContractError);
    expect(() => decodeAnalysisResponse({
      analysis_id: "7e89c1d1-c8cd-4e6c-b40c-e3ff65d644e3",
      repo_url: "https://github.com/acme/parser",
      issue_number: 42,
      status: "REVIEW_READY",
      progress: {},
      counters: {},
      created_at: "2026-08-30T08:00:00Z",
      updated_at: "2026-08-30T08:02:00Z",
      error: null,
      current_report: validReport,
      report_history: [
        { version: 1, report: validReport, created_at: "2026-08-30T08:01:00Z" },
        { version: 2, report: validReport, created_at: "2026-08-30T08:02:00Z" },
      ],
    })).toThrow(ContractError);
    expect(ANALYSIS_RESPONSE_MAX_BYTES).toBe(3 * 1024 * 1024);
  });
});
