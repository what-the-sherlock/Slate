import { afterEach, describe, expect, it } from "vitest";
import { clearInviteToken, readInviteToken } from "./invite";

const TOKEN = "AbC_def-0123456789abcdefXYZ";

afterEach(() => sessionStorage.clear());

describe("invite token", () => {
  it("reads it from the fragment and keeps it for this tab", () => {
    expect(readInviteToken(`#${TOKEN}`)).toBe(TOKEN);
    expect(readInviteToken("")).toBe(TOKEN); // after a sign-in detour, the hash is gone
    clearInviteToken();
    expect(readInviteToken("")).toBeNull();
  });
  it("ignores anything that isn't a token", () => {
    expect(readInviteToken("#short")).toBeNull();
    expect(readInviteToken("#<script>alert(1)</script>xxxxxxxxxxxxxxxx")).toBeNull();
    sessionStorage.setItem("slate.invite.token", "bad token with spaces");
    expect(readInviteToken("")).toBeNull();
  });
});
