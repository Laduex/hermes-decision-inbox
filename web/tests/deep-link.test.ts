import { describe, expect, it } from "vitest";

import { decisionIdFromSearch } from "../src/deep-link";


describe("Decision Inbox deep links", () => {
  it("opens a server-generated decision after authentication", () => {
    expect(decisionIdFromSearch("?decision=dec_a1B2c3")).toBe("dec_a1B2c3");
  });

  it("ignores missing or malformed decision ids", () => {
    expect(decisionIdFromSearch("?tab=archive")).toBeNull();
    expect(decisionIdFromSearch("?decision=../../state")).toBeNull();
  });
});
