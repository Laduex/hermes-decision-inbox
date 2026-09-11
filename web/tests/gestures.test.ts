import { describe, expect, it } from "vitest";
import { gestureAction } from "../src/gestures";

describe("gestureAction", () => {
  it("maps a right swipe to acceptance", () => expect(gestureAction(100, 4)).toBe("recommended"));
  it("maps a left swipe to rejection", () => expect(gestureAction(-100, 4)).toBe("rejected"));
  it("maps a dominant upward swipe to abstention", () => expect(gestureAction(5, -100)).toBe("abstained"));
  it("maps a dominant downward swipe to alternatives", () => expect(gestureAction(5, 100)).toBe("alternatives"));
  it("ignores short gestures", () => {
    expect(gestureAction(30, 30)).toBeNull();
  });
});
