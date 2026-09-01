import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { ApiError } from "../api/client";

describe("review feedback controls", () => {
  test("requires explicit acceptance confirmation and disables controls while pending", async () => {
    let finish: (() => void) | undefined;
    const submit = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    const { FeedbackControls } = await import("./FeedbackControls");
    render(<FeedbackControls revisionUsed={false} onSubmit={submit} />);

    fireEvent.click(screen.getByRole("button", { name: "接受报告" }));
    expect(submit).not.toHaveBeenCalled();
    expect(screen.getByText("确认后，本次分析将进入完成状态。")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认接受" }));
    expect(screen.getByRole("button", { name: "确认接受" })).toBeDisabled();
    finish?.();
    await waitFor(() => expect(submit).toHaveBeenCalledWith({ action: "accept" }));
  });

  test("rejects blank revision and exposes only the one allowed revision", async () => {
    const submit = vi.fn().mockResolvedValue(undefined);
    const { FeedbackControls } = await import("./FeedbackControls");
    const { rerender } = render(<FeedbackControls revisionUsed={false} onSubmit={submit} />);

    fireEvent.change(screen.getByLabelText("修订说明"), { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "请求修订" }));
    expect(await screen.findByText("请填写非空修订说明。")).toBeInTheDocument();
    expect(submit).not.toHaveBeenCalled();

    rerender(<FeedbackControls revisionUsed onSubmit={submit} />);
    expect(screen.getByText("一次修订机会已使用。")).toBeInTheDocument();
    expect(screen.queryByLabelText("修订说明")).not.toBeInTheDocument();
  });

  test("shows a safe conflict without reflecting server detail", async () => {
    const submit = vi.fn().mockRejectedValue(
      new ApiError("conflict", "当前分析状态与此操作冲突，请刷新后重试。", 409),
    );
    const { FeedbackControls } = await import("./FeedbackControls");
    render(<FeedbackControls revisionUsed={false} onSubmit={submit} />);

    fireEvent.change(screen.getByLabelText("修订说明"), { target: { value: "检查边界判断。" } });
    fireEvent.click(screen.getByRole("button", { name: "请求修订" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("当前分析状态与此操作冲突，请刷新后重试。");
  });
});
