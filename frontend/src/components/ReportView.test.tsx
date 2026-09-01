import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { validReport } from "../test/fixtures";

describe("investigation report", () => {
  test("renders every report section and a validated immutable GitHub permalink", async () => {
    const { ReportView } = await import("./ReportView");
    render(<ReportView report={validReport} repoUrl="https://github.com/acme/parser" />);

    expect(screen.getByRole("heading", { name: "调查报告" })).toBeInTheDocument();
    expect(screen.getByText("根因候选已识别")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "主要假设" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "证据" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "受影响文件" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "实施步骤" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "建议测试" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "不确定项" })).toBeInTheDocument();
    expect(screen.getByText("if cursor >= end:")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /在 GitHub 查看 src\/parser.py 第 14 到 17 行/ })).toHaveAttribute(
      "href",
      "https://github.com/acme/parser/blob/0123456789abcdef0123456789abcdef01234567/src/parser.py#L14-L17",
    );
  });

  test("uses meaningful evidence-insufficient empty states", async () => {
    const { ReportView } = await import("./ReportView");
    render(
      <ReportView
        repoUrl="https://github.com/example/parser"
        report={{
          ...validReport,
          outcome: "insufficient_evidence",
          primary_hypothesis: null,
          alternative_hypotheses: [],
          evidence: [],
          impacted_files: [],
          implementation_steps: [],
          proposed_tests: [],
        }}
      />,
    );

    expect(screen.getByText("证据不足")).toBeInTheDocument();
    expect(screen.getByText("当前没有通过不可变快照校验的代码证据。")).toBeInTheDocument();
    expect(screen.getByText("没有足够证据形成主要假设。")).toBeInTheDocument();
    expect(screen.getByText("暂不建议修改文件。")).toBeInTheDocument();
  });

  test("redacts credential shapes in code excerpts without changing line count", async () => {
    const { decodeAnalysisReport } = await import("../contracts");
    const { ReportView } = await import("./ReportView");
    const report = decodeAnalysisReport({
      ...validReport,
      evidence: [{
        ...validReport.evidence[0],
        excerpt: "OPENAI_API_KEY=sk-live-example-token\nAuthorization: Bearer ghp_abcdefghijklmnop\nurl='https://user:pass@example.test/path'",
      }],
    });
    render(<ReportView report={report} repoUrl="https://github.com/acme/parser" />);

    const excerpt = screen.getByLabelText("src/parser.py 代码摘录");
    expect(excerpt.querySelector("code")?.children).toHaveLength(3);
    expect(excerpt).toHaveTextContent("[已隐藏凭据]");
    expect(excerpt.textContent).not.toContain("example-token");
    expect(excerpt.textContent).not.toContain("abcdefghijklmnop");
    expect(excerpt.textContent).not.toContain("user:pass");
  });
});
