import { render, screen, within } from "@testing-library/react";
import { describe, expect, test } from "vitest";

describe("status timeline", () => {
  test("always exposes all eight exact backend states with text meaning", async () => {
    const { StatusTimeline } = await import("./StatusTimeline");
    render(<StatusTimeline currentStatus="REVISING" />);

    const timeline = within(screen.getByRole("list", { name: "分析状态时间线" }));
    for (const label of ["排队", "获取仓库", "建立索引", "静态调查", "等待确认", "修订中", "已完成", "失败"]) {
      expect(timeline.getByText(label)).toBeInTheDocument();
    }
    expect(timeline.getByText("修订中").closest("li")).toHaveTextContent("当前状态");
  });

  test("presents a safe future state without treating it as a known transition", async () => {
    const { StatusTimeline } = await import("./StatusTimeline");
    render(<StatusTimeline currentStatus="PAUSED_FOR_REVIEW" />);

    expect(screen.getByText("未知状态：PAUSED_FOR_REVIEW")).toBeInTheDocument();
  });

  test("does not fabricate REVISING on a direct REVIEW_READY to COMPLETED branch", async () => {
    const { StatusTimeline } = await import("./StatusTimeline");
    render(<StatusTimeline currentStatus="COMPLETED" observedStatuses={["QUEUED", "REVIEW_READY", "COMPLETED"]} />);

    expect(screen.getByText("修订中").closest("li")).toHaveClass("is-unobserved");
    expect(screen.getByText("等待确认").closest("li")).toHaveClass("is-observed");
  });

  test("does not imply successful stages for a failed analysis", async () => {
    const { StatusTimeline } = await import("./StatusTimeline");
    render(<StatusTimeline currentStatus="FAILED" observedStatuses={["QUEUED", "INGESTING", "FAILED"]} />);

    expect(screen.getByText("建立索引").closest("li")).toHaveClass("is-unobserved");
    expect(within(screen.getByRole("list", { name: "分析状态时间线" })).getByText("失败").closest("li")).toHaveClass("is-current");
  });
});
