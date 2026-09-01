import { describe, expect, test } from "vitest";

describe("bundled placeholder walkthroughs", () => {
  test("ships exactly three versioned and truthfully insufficient-evidence cases", async () => {
    const { bundledDemos } = await import("./artifacts");

    expect(bundledDemos.version).toBe("1");
    expect(bundledDemos.cases).toHaveLength(3);
    expect(bundledDemos.cases.map((item) => item.case_id)).toEqual([
      "parser-boundary",
      "cache-invalidation",
      "async-cleanup",
    ]);
    for (const demo of bundledDemos.cases) {
      expect(demo.repo_url).toMatch(/^https:\/\/github\.com\/example\//);
      expect(demo.report.outcome).toBe("insufficient_evidence");
      expect(demo.report.primary_hypothesis).toBeNull();
      expect(demo.report.uncertainties.join(" ")).toMatch(/流程演练|未验证/);
      expect(demo.events.length).toBeGreaterThan(2);
    }
  });

  test("steps through events in order before revealing the report", async () => {
    const { bundledDemos } = await import("./artifacts");
    const { createPlaybackState, demoPlaybackReducer } = await import("./player");
    const demo = bundledDemos.cases[0];
    let state = createPlaybackState();

    for (let index = 0; index < demo.events.length; index += 1) {
      state = demoPlaybackReducer(state, { type: "step", eventCount: demo.events.length });
      expect(state.visibleEventCount).toBe(index + 1);
      expect(state.reportVisible).toBe(index + 1 === demo.events.length);
    }

    expect(demo.events.slice(0, state.visibleEventCount).map((event) => event.sequence)).toEqual(
      demo.events.map((event) => event.sequence),
    );
  });

  test("play, pause, and restart stay deterministic under a controllable clock", async () => {
    const { createPlaybackState, demoPlaybackReducer } = await import("./player");
    let state = demoPlaybackReducer(createPlaybackState(), { type: "play" });
    state = demoPlaybackReducer(state, { type: "tick", eventCount: 5 });
    state = demoPlaybackReducer(state, { type: "pause" });
    const paused = demoPlaybackReducer(state, { type: "tick", eventCount: 5 });
    const restarted = demoPlaybackReducer(paused, { type: "restart" });

    expect(state).toMatchObject({ playing: false, visibleEventCount: 1, reportVisible: false });
    expect(paused.visibleEventCount).toBe(1);
    expect(restarted).toEqual(createPlaybackState());
  });
});
