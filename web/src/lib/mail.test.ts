import { describe, expect, it } from "vitest";
import type { MailProvider } from "@/lib/api/hooks";
import { guessProvider, looksLikeAddress, normaliseAppPassword } from "./mail";

const PROVIDERS: MailProvider[] = [
  { id: "gmail", name: "Gmail", app_password_url: "u", domains: ["gmail.com", "googlemail.com"] },
  { id: "icloud", name: "iCloud Mail", app_password_url: "u", domains: ["icloud.com", "me.com"] },
];

describe("mail helpers", () => {
  it("guesses the provider from the domain", () => {
    expect(guessProvider("Ana@GMAIL.com", PROVIDERS)).toBe("gmail");
    expect(guessProvider("a@me.com", PROVIDERS)).toBe("icloud");
    expect(guessProvider("a@company.dev", PROVIDERS)).toBeNull();
    expect(guessProvider("nope", PROVIDERS)).toBeNull();
  });
  it("checks address shape", () => {
    expect(looksLikeAddress(" a.b+c@mail.example.com ")).toBe(true);
    expect(looksLikeAddress("a@b")).toBe(false);
    expect(looksLikeAddress("a b@c.com")).toBe(false);
  });
  it("drops the spaces providers show in app passwords", () => {
    expect(normaliseAppPassword("abcd efgh\tijkl mnop")).toBe("abcdefghijklmnop");
  });
});
