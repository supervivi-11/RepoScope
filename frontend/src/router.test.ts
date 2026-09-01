import { expect, test } from "vitest";

import { analysisHref, parseHash } from "./router";

test("canonicalizes analysis UUID hashes to lowercase", () => {
  const upper = "7E89C1D1-C8CD-4E6C-B40C-E3FF65D644E3";
  expect(parseHash(`#analysis/${upper}`)).toEqual({
    kind: "analysis",
    analysisId: "7e89c1d1-c8cd-4e6c-b40c-e3ff65d644e3",
  });
  expect(analysisHref(upper)).toBe("#analysis/7e89c1d1-c8cd-4e6c-b40c-e3ff65d644e3");
});
