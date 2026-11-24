import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { makeQueryClient } from "@/app/providers";
import { SetupForm, stepProblem } from "./setup-form";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));

function json(body: unknown, status = 200) {
  const type = status >= 400 ? "application/problem+json" : "application/json";
  return new Response(JSON.stringify(body), { status, headers: { "content-type": type } });
}

function mockApi({ token = false, setup }: { token?: boolean; setup?: () => Response } = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      const path = new URL(req.url).pathname;
      if (path === "/api/v1/auth/status") return json({ set_up: false, requires_setup_token: token });
      if (path === "/api/v1/auth/setup" && setup) return setup();
      return json({}, 404);
    }),
  );
}

function renderForm() {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <SetupForm />
    </QueryClientProvider>,
  );
}

const next = () => fireEvent.click(screen.getByRole("button", { name: /continue|create workspace/i }));

async function answer(label: RegExp, value: string) {
  fireEvent.change(await screen.findByLabelText(label), { target: { value } });
  next();
}

afterEach(() => {
  vi.unstubAllGlobals();
  replace.mockReset();
});

describe("SetupForm", () => {
  it("asks one question at a time and keeps answered fields in the form for autofill", async () => {
    mockApi();
    renderForm();
    expect(await screen.findByText("Step 1 of 4")).toBeInTheDocument();
    await answer(/name your workspace/i, "Acme");
    expect(await screen.findByText("Step 2 of 4")).toBeInTheDocument();
    // The workspace input is still there (hidden), with its autocomplete hint.
    expect(document.querySelector('input[name="workspace_name"]')).toHaveValue("Acme");
    expect(document.querySelector('input[autocomplete="organization"]')).not.toBeNull();
  });

  it("stops on an invalid email and a weak password before calling the server", async () => {
    mockApi();
    renderForm();
    await answer(/name your workspace/i, "Acme");
    await answer(/your name/i, "Ada");
    await answer(/work email/i, "not-an-email");
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter an email like name@company.com.");
    await answer(/work email/i, "ada@acme.dev");
    await answer(/create a password/i, "short");
    expect(await screen.findByRole("alert")).toHaveTextContent("doesn't meet the rules");
  });

  it("asks for the setup code only when the server requires one", async () => {
    mockApi({ token: true });
    renderForm();
    expect(await screen.findByText("Step 1 of 5")).toBeInTheDocument();
  });

  it("jumps back to the step a server error belongs to", async () => {
    mockApi({
      setup: () =>
        json(
          { type: "/problems/validation", title: "Invalid request", status: 422, fields: { email: "That email can't be used." } },
          422,
        ),
    });
    renderForm();
    await answer(/name your workspace/i, "Acme");
    await answer(/your name/i, "Ada");
    await answer(/work email/i, "ada@acme.dev");
    await answer(/create a password/i, "correct horse battery");
    expect(await screen.findByText("Step 3 of 4")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("That email can't be used.");
    expect(replace).not.toHaveBeenCalled();
  });
});

describe("stepProblem", () => {
  it("lets a step move on only when its value is acceptable", () => {
    expect(stepProblem("workspace_name", "  ")).not.toBeNull();
    expect(stepProblem("email", "ada@acme.dev")).toBeNull();
    expect(stepProblem("password", "correct horse battery")).toBeNull();
    expect(stepProblem("password", " correct horse battery")).not.toBeNull();
  });
});
