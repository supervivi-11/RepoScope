import type { AnalysisReport } from "../contracts";

export const validReport: AnalysisReport = {
  outcome: "root_cause_identified",
  issue_summary: "The parser drops the terminal token.",
  observed_behavior: "Parsing returns an incomplete node.",
  expected_behavior: "Parsing returns the complete node.",
  primary_hypothesis: {
    statement: "The boundary guard stops one token early.",
    confidence: 0.86,
    evidence: [
      {
        commit_sha: "0123456789abcdef0123456789abcdef01234567",
        path: "src/parser.py",
        start_line: 14,
        end_line: 17,
      },
    ],
  },
  alternative_hypotheses: [],
  evidence: [
    {
      commit_sha: "0123456789abcdef0123456789abcdef01234567",
      path: "src/parser.py",
      start_line: 14,
      end_line: 17,
      excerpt: "if cursor >= end:\n    return None",
      explanation: "The guard exits before consuming the terminal token.",
    },
  ],
  impacted_files: [
    { path: "src/parser.py", explanation: "Owns the boundary guard." },
  ],
  implementation_steps: ["Correct the guard after confirming the token contract."],
  proposed_tests: [
    { name: "terminal token", description: "Covers the inclusive boundary." },
  ],
  uncertainties: ["The lexer contract still needs confirmation."],
  confidence: 0.86,
};

export const validAnalysisResponse = {
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
} as const;
