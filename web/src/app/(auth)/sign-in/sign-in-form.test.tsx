import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { makeQueryClient } from "@/app/providers";
import { SIGN_IN_TIMEOUT_MS, SLOW_AFTER_MS, SignInForm } from "./sign-in-form";

const replace = vi.fn();
const prefetch = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, prefetch }),
  useSearchParams: () => new URLSearchParams(),
}));

/** A login endpoint that never answers, but rejects like fetch does when its signal aborts. */
function mockHangingLogin() {
  vi.stubGlobal(
    "fetch",
    vi.fn((req: Request) => {
      return new Promise<Response>((_, reject) => {
        req.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      });
    }),
  );
}

async function reachPasswordStep() {
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "ada@acme.dev" } });
  fireEvent.click(screen.getByRole("button", { name: /continue/i }));
  fireEvent.change(await screen.findByLabelText("Password"), { target: { value: "a password" } });
}

const ME = {
  user: { id: "u", email: "ada@acme.dev", name: "Ada" },
  workspace_id: "w",
  role: "owner",
  is_platform_admin: false,
};

function mockApi(loginStatus: number, { verified = true }: { verified?: boolean } = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      const path = new URL(req.url).pathname;
      if (path === "/api/v1/auth/login" && loginStatus === 200)
        return new Response(JSON.stringify({ ...ME, email_verified: verified }), {
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
  vi.useRealTimers();
  vi.unstubAllGlobals();
  replace.mockReset();
  prefetch.mockReset();
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

  it("prefetches the destination once the password step opens", async () => {
    mockApi(401);
    renderForm();
    await reachPasswordStep();
    expect(prefetch).toHaveBeenCalledWith("/home");
  });

  it("says the server is waking up when sign-in is slow, then gives up and re-enables the form", async () => {
    mockHangingLogin();
    renderForm();
    await reachPasswordStep();
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await act(() => vi.advanceTimersByTimeAsync(SLOW_AFTER_MS));
    expect(screen.getByText(/waking up the server/i)).toBeInTheDocument();

    await act(() => vi.advanceTimersByTimeAsync(SIGN_IN_TIMEOUT_MS - SLOW_AFTER_MS));
    expect(screen.getByRole("alert")).toHaveTextContent("The server took too long to respond. Try again.");
    expect(screen.queryByText(/waking up the server/i)).toBeNull();
    expect(screen.getByLabelText("Password")).toHaveValue("");
    expect(replace).not.toHaveBeenCalled();
  });

  it("links to sign-up and to password reset; there is no guest demo any more", async () => {
    mockApi(401);
    renderForm();
    expect(screen.getByRole("link", { name: /create one free/i })).toHaveAttribute("href", "/sign-up");
    expect(screen.queryByText(/live demo/i)).toBeNull();
    await reachPasswordStep();
    expect(screen.getByRole("link", { name: /forgot password/i })).toHaveAttribute(
      "href",
      "/forgot-password?email=ada%40acme.dev",
    );
  });

  it("sends a verified account to its destination", async () => {
    mockApi(200);
    renderForm();
    await reachPasswordStep();
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await vi.waitFor(() => expect(replace).toHaveBeenCalledWith("/home"));
  });

  it("sends an unverified account to verify its email first", async () => {
    mockApi(200, { verified: false });
    renderForm();
    await reachPasswordStep();
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await vi.waitFor(() => expect(replace).toHaveBeenCalledWith("/verify"));
  });
});
