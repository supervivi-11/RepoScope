import { expect, test, type Page } from "@playwright/test";

const analysisId = "7e89c1d1-c8cd-4e6c-b40c-e3ff65d644e3";
const commit = "0123456789abcdef0123456789abcdef01234567";

const report = {
  outcome: "root_cause_identified",
  issue_summary: "The parser drops the terminal token.",
  observed_behavior: "Parsing returns an incomplete node.",
  expected_behavior: "Parsing returns the complete node.",
  primary_hypothesis: {
    statement: "The boundary guard stops one token early.",
    confidence: 0.86,
    evidence: [
      {
        commit_sha: commit,
        path: "src/parser.py",
        start_line: 14,
        end_line: 17,
      },
    ],
  },
  alternative_hypotheses: [],
  evidence: [
    {
      commit_sha: commit,
      path: "src/parser.py",
      start_line: 14,
      end_line: 17,
      excerpt: "if cursor >= end:\n    return None",
      explanation: "The guard exits before consuming the terminal token.",
    },
  ],
  impacted_files: [{ path: "src/parser.py", explanation: "Owns the boundary guard." }],
  implementation_steps: ["Correct the guard after confirming the token contract."],
  proposed_tests: [{ name: "terminal token", description: "Covers the inclusive boundary." }],
  uncertainties: ["The lexer contract still needs confirmation."],
  confidence: 0.86,
};

function analysis(status: string, withReport: boolean) {
  return {
    analysis_id: analysisId,
    repo_url: "https://github.com/acme/parser",
    issue_number: 42,
    status,
    progress: { commit_sha: commit },
    counters: { tool_calls: 3, evidence_rounds: 1, model_attempts: 1 },
    created_at: "2026-08-30T08:00:00Z",
    updated_at: "2026-08-30T08:02:00Z",
    error: null,
    current_report: withReport ? report : null,
    report_history: withReport
      ? [{ version: 1, report, created_at: "2026-08-30T08:02:00Z" }]
      : [],
  };
}

async function rejectUnexpectedExternalTraffic(page: Page) {
  await page.route(/^https?:\/\/(?!127\.0\.0\.1:4173).*$/, async (route) => {
    throw new Error(`Unexpected external request: ${route.request().url()}`);
  });
}

test("@static landing → bundled demo → ordered playback → report uses zero API requests", async ({ page }) => {
  const apiRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/") || request.url().includes("github.com")) apiRequests.push(request.url());
  });
  await rejectUnexpectedExternalTraffic(page);
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "把陌生仓库，变成可验证的调查路径。" })).toBeVisible();
  await page.getByRole("link", { name: /打开解析器边界/ }).click();
  await expect(page).toHaveURL(/#demo\/parser-boundary$/);
  await expect(page.getByText("架构与产品流程占位演练，不是真实基准结果。")).toBeVisible();
  await page.getByRole("button", { name: "播放" }).click();
  await expect(page.getByRole("heading", { name: "调查报告" })).toBeVisible({ timeout: 8_000 });
  await expect(page.getByText("证据不足", { exact: true })).toBeVisible();
  expect(apiRequests).toEqual([]);
});

test("@live local form → mocked 202 → SSE reconnect → review report → accept", async ({ page }) => {
  let analysisReads = 0;
  let streamReads = 0;
  let accepted = false;
  let postFeedbackReads = 0;
  const reconnectHeaders: Array<string | null> = [];

  await rejectUnexpectedExternalTraffic(page);
  await page.route(/\/api\/v1\/analyses$/, async (route) => {
    expect(route.request().method()).toBe("POST");
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ analysis_id: analysisId, status: "QUEUED" }),
    });
  });
  await page.route(new RegExp(`/api/v1/analyses/${analysisId}/feedback$`), async (route) => {
    accepted = true;
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({ analysis_id: analysisId, status: "REVIEW_READY" }),
    });
  });
  await page.route(new RegExp(`/api/v1/analyses/${analysisId}/events$`), async (route) => {
    streamReads += 1;
    reconnectHeaders.push(route.request().headers()["last-event-id"] ?? null);
    const body = streamReads === 1
      ? 'id: 1\nevent: ingestion_started\ndata: {"status":"INGESTING"}\n\n'
      : accepted
        ? 'id: 3\nevent: report_accepted\ndata: {"status":"COMPLETED"}\n\n'
        : 'id: 2\nevent: review_ready\ndata: {"status":"REVIEW_READY"}\n\n';
    await route.fulfill({ status: 200, contentType: "text/event-stream", body });
  });
  await page.route(new RegExp(`/api/v1/analyses/${analysisId}$`), async (route) => {
    analysisReads += 1;
    const ready = analysisReads > 1;
    if (accepted) postFeedbackReads += 1;
    const completed = accepted && postFeedbackReads > 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(completed ? analysis("COMPLETED", true) : analysis(ready || accepted ? "REVIEW_READY" : "QUEUED", ready || accepted)),
    });
  });

  await page.goto("/");
  await page.getByLabel("GitHub 仓库 URL").fill("https://github.com/acme/parser");
  await page.getByLabel("Issue 编号").fill("42");
  await page.getByRole("button", { name: "开始静态调查" }).click();

  await expect(page).toHaveURL(new RegExp(`#analysis/${analysisId}$`));
  await expect(page.getByRole("heading", { name: "调查报告" })).toBeVisible({ timeout: 8_000 });
  await expect.poll(() => reconnectHeaders).toContain("1");
  await page.getByRole("button", { name: "接受报告" }).click();
  await page.getByRole("button", { name: "确认接受" }).click();
  await expect(page.locator(".status-pill")).toContainText("已完成");
});

test("@static a refreshed static hash route restores the demo", async ({ page }) => {
  await rejectUnexpectedExternalTraffic(page);
  await page.goto("/#demo/cache-invalidation");
  await page.reload();

  await expect(page.getByRole("heading", { name: "缓存失效 · 产品流程演练" })).toBeVisible();
  await expect(page.getByText("预生成演示").first()).toBeVisible();
});

test("@static 360px layout supports keyboard focus without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await rejectUnexpectedExternalTraffic(page);
  await page.goto("/");
  await page.keyboard.press("Tab");

  await expect(page.locator(".skip-link")).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.keyboard.press("Enter");
  await expect(page.locator("#main-content")).toBeInViewport();
});
