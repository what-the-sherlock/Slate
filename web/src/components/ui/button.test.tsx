import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { cn } from "@/lib/utils";
import { Button } from "./button";

// Regression: the bare `cn` package read text-ui / text-caption as colours and dropped
// text-white, so primary buttons showed dark labels on blue.
describe("Button", () => {
  it.each(["sm", "md", "lg"] as const)("keeps white text on a primary %s button", (size) => {
    render(
      <Button variant="primary" size={size}>
        Sign in
      </Button>,
    );
    const cls = screen.getByRole("button", { name: "Sign in" }).className;
    expect(cls).toContain("text-white");
    expect(cls).toMatch(/text-(caption|ui)/);
  });
});

describe("cn", () => {
  it("treats the role type scale as font sizes, not colours", () => {
    expect(cn("text-white", "text-ui")).toBe("text-white text-ui");
    expect(cn("text-data text-fg", "text-brand")).toBe("text-data text-brand");
    expect(cn("text-caption", "text-body")).toBe("text-body");
  });
});
