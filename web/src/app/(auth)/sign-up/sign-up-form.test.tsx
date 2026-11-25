import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { makeQueryClient } from "@/app/providers";
import { DEV_TOKEN } from "@/components/turnstile";
import { SignUpForm, stepProblem } from "./sign-up-form";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));

function json(body: unknown, status = 200) {
  const type = status >= 400 ? "application/problem+json" : "application/json";
  return new Response(JSON.stringify(body), { status, headers: { "content-type": type } });
}

const ME = {
  user: { id: "u", email: "ada@acme.dev", name: "Ada" },
  workspace_id: "w",
  role: "owner",
  email_verified: false,
  is_platform_admin: false,
};

function mockApi({ open = true, signup }: { open?: boolean; signup?: (body: Record<string, unknown>) => Response } = {}) {
  const bodies: Record<string, unknown>[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      const path = new URL(req.url).pathname;
      if (path === "/api/v1/auth/config") return json({ signups_enabled: open, turnstile_site_key: null });
      if (path === "/api/v1/auth/signup") {
        const body = (await req.json()) as Record<string, unknown>;
        bodies.push(body);
        return signup ? signup(body) : json(ME, 201);
      }
      return json({}, 404);
    }),
  );
  return bodies;
}

function renderForm() {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <SignUpForm />
    </QueryClientProvider>,
  );
}

const next = () => fireEvent.click(screen.getByRole("button", { name: /continue|create account/i }));

async function answer(label: RegExp, value: string) {
  fireEvent.change(await screen.findByLabelText(label), { target: { value } });
  next();
}

async function fillAll() {
  await answer(/your name/i, "Ada");
  await answer(/your email/i, "ada@acme.dev");
  await answer(/create a password/i, "correct horse battery");
}

afterEach(() => {
  vi.unstubAllGlobals();
  replace.mockReset();
});

describe("SignUpForm", () => {
  it("never asks for a workspace: name, email, password", async () => {
    mockApi();
    renderForm();
    expect(await screen.findByText("Step 1 of 3")).toBeInTheDocument();
    expect(document.querySelector('input[name="workspace_name"]')).toBeNull();
    await answer(/your name/i, "Ada");
    expect(await screen.findByText("Step 2 of 3")).toBeInTheDocument();
    // The name input is still there (hidden), with its autocomplete hint.
    expect(document.querySelector('input[autocomplete="name"]')).toHaveValue("Ada");
  });

  it("stops on an invalid email and a weak password before calling the server", async () => {
    const bodies = mockApi();
    renderForm();
    await answer(/your name/i, "Ada");
    await answer(/your email/i, "not-an-email");
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter an email like name@company.com.");
    await answer(/your email/i, "ada@acme.dev");
    await answer(/create a password/i, "short");
    expect(await screen.findByRole("alert")).toHaveTextContent("doesn't meet the rules");
    expect(bodies).toHaveLength(0);
  });

  it("sends the bot-check token and an empty honeypot, then goes to verify", async () => {
    const bodies = mockApi();
    renderForm();
    await fillAll();
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/verify"));
    expect(bodies[0]).toMatchObject({ email: "ada@acme.dev", turnstile_token: DEV_TOKEN, website: "" });
  });

  it("asks for the setup code only when the server says the address is the owner's", async () => {
    let calls = 0;
    mockApi({
      signup: () =>
        ++calls === 1
          ? json({ type: "/problems/validation", title: "Invalid", status: 422, fields: { setup_token: "Enter the setup code for this address." } }, 422)
          : json(ME, 201),
    });
    renderForm();
    await fillAll();
    expect(await screen.findByText("Step 4 of 4")).toBeInTheDocument();
    await answer(/setup code/i, "s3tup");
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/verify"));
  });

  it("jumps back to the step a server error belongs to", async () => {
    mockApi({
      signup: () =>
        json({ type: "/problems/conflict", title: "Conflict", status: 409, fields: { email: "This email already has an account." } }, 409),
    });
    renderForm();
    await fillAll();
    expect(await screen.findByText("Step 2 of 3")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("already has an account");
    expect(replace).not.toHaveBeenCalled();
  });

  it("says so when sign-up is closed", async () => {
    mockApi({ open: false });
    renderForm();
    expect(await screen.findByText(/sign-ups are closed/i)).toBeInTheDocument();
  });
});

describe("stepProblem", () => {
  it("lets a step move on only when its value is acceptable", () => {
    expect(stepProblem("name", "  ")).not.toBeNull();
    expect(stepProblem("email", "ada@acme.dev")).toBeNull();
    expect(stepProblem("password", "correct horse battery")).toBeNull();
    expect(stepProblem("password", " correct horse battery")).not.toBeNull();
  });
});
