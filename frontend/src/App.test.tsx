import { render, screen } from "@testing-library/react";

import App from "./App";

test("states RepoScope's static read-only analysis boundary", () => {
  render(<App />);

  expect(screen.getByRole("heading", { name: "RepoScope" })).toBeInTheDocument();
  expect(
    screen.getByText(/v1 performs static, read-only analysis and never executes repository code/i),
  ).toBeInTheDocument();
});
