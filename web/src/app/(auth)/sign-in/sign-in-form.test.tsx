import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { makeQueryClient } from "@/app/providers";
import { SignInForm } from "./sign-in-form";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  useSearchParams: () => new URLSearchParams(),
}));

function mockApi(loginStatus: number) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      const path = new URL(req.url).pathname;
      if (path === "/api/v1/auth/status")
        return new Response(JSON.stringify({ set_up: true, requires_setup_token: false }), {
          headers: { "content-type": "application/json" },
        });
      if (path === "/api/v1/auth/login")
        return new Response(
          JSON.stringify({ type: "/problems/unauthenticated", title: "Authentication required", status: 401, detail: "Email or password is incorrect." }),
          { status: loginStatus, headers: { "content-type": "application/problem+json" } },
        );
      return new Response("{}", { status: 404 });
    }),
  );
}

function renderForm() {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <SignInForm />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  replace.mockReset();
});

describe("SignInForm", () => {
  it("validates the email before showing the password step", async () => {
    mockApi(401);
    renderForm();
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "not-an-email" } });
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));
    expect(await screen.findByText("Enter an email like name@company.com.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Password")).toBeNull();
  });

  it("shows one generic message when sign-in fails", async () => {
    mockApi(401);
    renderForm();
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "ada@acme.dev" } });
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));
    fireEvent.change(await screen.findByLabelText("Password"), { target: { value: "wrong password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Email or password is incorrect.")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalledWith("/sources");
  });

  it("keeps the username field in the form for password managers", async () => {
    mockApi(401);
    renderForm();
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "ada@acme.dev" } });
    fireEvent.click(screen.getByRole("button", { name: /continue/i }));
    await screen.findByLabelText("Password");
    expect(document.querySelector('input[autocomplete="username"]')).not.toBeNull();
    expect(document.querySelector('input[autocomplete="current-password"]')).not.toBeNull();
  });

  it("tints Continue until the email looks valid, then makes it solid", async () => {
    mockApi(401);
    renderForm();
    const button = screen.getByRole("button", { name: /continue/i });
    expect(button).toHaveAttribute("data-state", "idle");
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "ada@acme.dev" } });
    expect(button).toHaveAttribute("data-state", "ready");
  });

  it("offers the read-only demo instead of an open sign-up", () => {
    mockApi(401);
    renderForm();
    expect(screen.getByRole("button", { name: /explore the live demo/i })).toBeInTheDocument();
  });
});
