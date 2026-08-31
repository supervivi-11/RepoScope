import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, test, vi } from "vitest";

import App from "./App";
import { createApiClient } from "./api/client";

afterEach(() => {
  window.location.hash = "";
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

describe("RepoScope investigation entry", () => {
  test("presents distinct local-live and truthful pre-generated paths", () => {
    render(<App mode="live" />);

    expect(screen.getByRole("heading", { name: "把陌生仓库，变成可验证的调查路径。" })).toBeInTheDocument();
    expect(screen.getByText(/Evidence-first investigation workspace/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "本地实时分析" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "预生成演示" })).toBeInTheDocument();
    expect(screen.getAllByText("预生成演示")).toHaveLength(4);
    expect(screen.getByText(/不会执行仓库代码/)).toBeInTheDocument();
  });

  test("validates public GitHub input and a positive Issue before any request", async () => {
    const network = vi.fn<typeof fetch>();
    render(<App mode="live" apiClient={createApiClient({ fetch: network })} streamFetch={network} />);

    fireEvent.change(screen.getByLabelText("GitHub 仓库 URL"), { target: { value: "https://example.com/acme/repo" } });
    fireEvent.change(screen.getByLabelText("Issue 编号"), { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "开始静态调查" }));

    expect(await screen.findByText("请输入公开 GitHub 仓库 URL。")).toBeInTheDocument();
    expect(screen.getByText("Issue 编号必须是正整数。")).toBeInTheDocument();
    expect(network).not.toHaveBeenCalled();
  });

  test("submits a live analysis and opens its hash-restorable workspace", async () => {
    const analysisId = "7e89c1d1-c8cd-4e6c-b40c-e3ff65d644e3";
    const apiFetch = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ analysis_id: analysisId, status: "QUEUED" }), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValue(
        new Response(
          JSON.stringify({
            analysis_id: analysisId,
            repo_url: "https://github.com/acme/parser",
            issue_number: 42,
            status: "QUEUED",
            progress: {},
            counters: {},
            created_at: "2026-08-30T08:00:00Z",
            updated_at: "2026-08-30T08:00:00Z",
            error: null,
            current_report: null,
            report_history: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    const streamFetch = vi.fn<typeof fetch>().mockImplementation(
      (_input, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        }),
    );
    render(<App mode="live" apiClient={createApiClient({ fetch: apiFetch })} streamFetch={streamFetch} />);

    fireEvent.change(screen.getByLabelText("GitHub 仓库 URL"), { target: { value: "https://github.com/acme/parser" } });
    fireEvent.change(screen.getByLabelText("Issue 编号"), { target: { value: "42" } });
    fireEvent.click(screen.getByRole("button", { name: "开始静态调查" }));

    expect(await screen.findByRole("heading", { name: "调查工作区" })).toBeInTheDocument();
    expect(window.location.hash).toBe(`#analysis/${analysisId}`);
    expect(screen.getByText("acme/parser")).toBeInTheDocument();
    expect(apiFetch.mock.calls[0][1]?.method).toBe("POST");
  });

  test("plays a bundled demo to its report with provably zero network calls", async () => {
    const network = vi.fn<typeof fetch>();
    const client = createApiClient({ baseUrl: "https://configured.example", fetch: network });
    render(<App apiClient={client} streamFetch={network} demoIntervalMs={100} />);

    fireEvent.click(screen.getByRole("link", { name: /打开解析器边界/ }));
    expect(await screen.findByText("架构与产品流程占位演练，不是真实基准结果。")).toBeInTheDocument();
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "播放" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600);
    });

    expect(screen.getByRole("heading", { name: "调查报告" })).toBeInTheDocument();
    expect(screen.getByText("证据不足")).toBeInTheDocument();
    expect(network).not.toHaveBeenCalled();
  });

  test("restores a demo directly from a refreshed hash route", async () => {
    window.location.hash = "#demo/cache-invalidation";
    render(<App />);

    await waitFor(() => expect(screen.getByRole("heading", { name: "缓存失效 · 产品流程演练" })).toBeInTheDocument());
    expect(screen.getAllByText("预生成演示").length).toBeGreaterThan(0);
  });

  test("the skip link focuses main content without replacing the hash route", async () => {
    window.location.hash = "#demo/cache-invalidation";
    render(<App />);
    const skip = screen.getByRole("link", { name: "跳到主要内容" });

    fireEvent.click(skip);

    expect(window.location.hash).toBe("#demo/cache-invalidation");
    expect(document.querySelector("#main-content")).toHaveFocus();
  });

  test("production static mode cannot create, read, or stream live analyses", async () => {
    const network = vi.fn<typeof fetch>();
    const client = createApiClient({ baseUrl: "https://configured.example", fetch: network });
    window.location.hash = "#analysis/7e89c1d1-c8cd-4e6c-b40c-e3ff65d644e3";

    render(<App apiClient={client} streamFetch={network} />);

    expect(await screen.findByRole("heading", { name: "本地实时分析未启用" })).toBeInTheDocument();
    expect(network).not.toHaveBeenCalled();
  });

  test("reuses one idempotency key for a failed submission retry and rotates after input changes", async () => {
    const keys: Array<string | null> = [];
    const analysisId = "7e89c1d1-c8cd-4e6c-b40c-e3ff65d644e3";
    const apiFetch = vi.fn<typeof fetch>(async (_input, init) => {
      keys.push(new Headers(init?.headers).get("Idempotency-Key"));
      if (keys.length <= 2) throw new TypeError("offline");
      return new Response(JSON.stringify({ analysis_id: analysisId, status: "QUEUED" }), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      });
    });
    render(<App mode="live" apiClient={createApiClient({ fetch: apiFetch })} streamFetch={vi.fn()} />);
    const repo = screen.getByLabelText("GitHub 仓库 URL");
    const issue = screen.getByLabelText("Issue 编号");
    fireEvent.change(repo, { target: { value: "https://github.com/acme/parser" } });
    fireEvent.change(issue, { target: { value: "42" } });
    fireEvent.click(screen.getByRole("button", { name: "开始静态调查" }));
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "开始静态调查" }));
    await waitFor(() => expect(keys).toHaveLength(2));
    expect(keys[0]).toBe(keys[1]);
    fireEvent.change(issue, { target: { value: "43" } });
    fireEvent.click(screen.getByRole("button", { name: "开始静态调查" }));
    await waitFor(() => expect(keys).toHaveLength(3));
    expect(keys[2]).not.toBe(keys[1]);
  });

  test("resets deterministic demo playback when the hash changes to another case", async () => {
    window.location.hash = "#demo/parser-boundary";
    render(<App demoIntervalMs={10_000} />);
    fireEvent.click(await screen.findByRole("button", { name: "单步" }));
    expect(screen.getByText(/事件 1\/5/)).toBeInTheDocument();

    act(() => { window.location.hash = "#demo/cache-invalidation"; window.dispatchEvent(new HashChangeEvent("hashchange")); });

    expect(await screen.findByRole("heading", { name: "缓存失效 · 产品流程演练" })).toBeInTheDocument();
    expect(screen.getByText(/事件 0\/5/)).toBeInTheDocument();
  });

  test("keeps interface letter spacing neutral for readable compact panels", () => {
    const styles = readFileSync("src/styles.css", "utf8");
    const letterSpacingValues = [...styles.matchAll(/letter-spacing:\s*([^;]+);/g)].map((match) => match[1].trim());

    expect(letterSpacingValues).toEqual(letterSpacingValues.map(() => "0"));
  });
});
