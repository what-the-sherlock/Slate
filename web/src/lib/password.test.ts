import { describe, expect, it } from "vitest";
import { CODE, normaliseCode, passwordOk } from "./password";

// The hints must agree with core/domain/auth.py validate_password, or the form would show
// every rule met and the server would still reject the password.
describe("PASSWORD_RULES", () => {
  it.each([
    ["11 characters", "a".repeat(11), false],
    ["12 characters", "a".repeat(12), true],
    ["256 characters", "a".repeat(256), true],
    ["257 characters", "a".repeat(257), false],
    ["a leading space", " correct horse battery", false],
    ["a trailing space", "correct horse battery ", false],
    ["inner spaces only", "correct horse battery", true],
    ["empty", "", false],
  ])("%s -> %s", (_name, pw, expected) => {
    expect(passwordOk(pw)).toBe(expected);
  });
});

describe("one-time codes", () => {
  it("accepts six digits, including pasted ones with spaces", () => {
    expect(CODE.test(normaliseCode(" 042 137 "))).toBe(true);
    expect(CODE.test(normaliseCode("04213"))).toBe(false);
    expect(CODE.test(normaliseCode("04213a"))).toBe(false);
  });
});
