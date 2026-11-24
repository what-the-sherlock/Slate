"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Mail } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { api, ApiProblem, errorMessage, unwrap } from "@/lib/api/client";
import { keys, useSetupStatus } from "@/lib/api/hooks";
import { safeNext } from "@/lib/safe-next";

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

/**
 * Two-step sign-in in one <form> (UI_DESIGN §4.1): the email bar grows into a password
 * step. The email input stays mounted (autocomplete="username") so password managers work.
 */
export function SignInForm() {
  const router = useRouter();
  const params = useSearchParams();
  const client = useQueryClient();
  const status = useSetupStatus();
  const [step, setStep] = useState<"email" | "password">("email");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"signin" | "guest" | null>(null);
  const passwordRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (status.data && !status.data.set_up) router.replace("/setup");
  }, [status.data, router]);

  useEffect(() => {
    if (step === "password") passwordRef.current?.focus();
  }, [step]);

  const expired = params.get("reason") === "expired";

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (step === "email") {
      if (!EMAIL.test(email.trim())) return setError("Enter an email like name@company.com.");
      return setStep("password");
    }
    setBusy("signin");
    try {
      unwrap(await api.POST("/api/v1/auth/login", { body: { email: email.trim(), password } }));
      await client.invalidateQueries({ queryKey: keys.me });
      router.replace(safeNext(params.get("next"), window.location.origin));
    } catch (err) {
      setError(err instanceof ApiProblem && err.status === 401 ? "Email or password is incorrect." : errorMessage(err));
      setPassword("");
      setBusy(null);
    }
  }

  async function asGuest() {
    setError(null);
    setBusy("guest");
    try {
      unwrap(await api.POST("/api/v1/auth/guest"));
      await client.invalidateQueries({ queryKey: keys.me });
      router.replace("/sources");
    } catch (err) {
      setError(errorMessage(err));
      setBusy(null);
    }
  }

  return (
    <div className="text-left">
      {expired ? (
        <p role="status" className="mb-3 rounded-md border border-brand-border bg-brand-subtle px-3.5 py-2.5 text-[13px] text-brand-strong">
          Your session expired. Sign in again to continue.
        </p>
      ) : null}
      <form
        onSubmit={onSubmit}
        noValidate
        className="rounded-lg border border-brand-border bg-bg p-1.5 shadow-sm transition-[height] focus-within:border-brand"
      >
        <label htmlFor="email" className="sr-only">
          Email
        </label>
        <div className={step === "email" ? "flex items-center gap-2 pl-3.5" : "sr-only"}>
          <Mail className="size-4 shrink-0 text-fg-subtle" aria-hidden="true" />
          <input
            id="email"
            type="email"
            name="email"
            autoComplete="username"
            autoFocus
            placeholder="you@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? "signin-error" : undefined}
            className="h-11 min-w-0 flex-1 bg-transparent text-base text-fg outline-none placeholder:text-fg-subtle sm:text-[15px]"
          />
          <Button type="submit" variant="tinted" size="lg" className="shrink-0">
            <span className="hidden sm:inline">Continue</span>
            <ArrowRight className="size-4" aria-label="Continue" />
          </Button>
        </div>

        {step === "password" ? (
          <div className="flex flex-col gap-3 p-3">
            <div className="flex items-center justify-between text-sm">
              <span className="truncate text-fg">{email}</span>
              <button
                type="button"
                className="shrink-0 text-brand underline-offset-[3px] hover:underline"
                onClick={() => {
                  setStep("email");
                  setPassword("");
                  setError(null);
                }}
              >
                Change
              </button>
            </div>
            <label htmlFor="password" className="text-sm font-medium text-fg">
              Password
            </label>
            <input
              ref={passwordRef}
              id="password"
              type="password"
              name="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              aria-invalid={error ? true : undefined}
              aria-describedby={error ? "signin-error" : undefined}
              className="h-11 rounded-md border border-[var(--sl-border-input)] bg-bg px-3 text-base text-fg outline-none transition-colors focus:border-brand focus:shadow-[0_0_0_3px_var(--sl-accent-subtle)] sm:text-[15px]"
            />
            <Button type="submit" variant="primary" size="lg" loading={busy === "signin"} disabled={!password}>
              Sign in
            </Button>
          </div>
        ) : null}
      </form>
      <p id="signin-error" role="alert" className="mt-2 min-h-5 px-1 text-[13px] text-danger">
        {error}
      </p>
      <p className="mt-1 text-center text-[15px] text-fg-muted">
        No account?{" "}
        <button
          type="button"
          onClick={asGuest}
          disabled={busy !== null}
          className="group inline-flex items-center gap-1 font-medium text-brand underline-offset-[3px] hover:underline disabled:opacity-50"
        >
          {busy === "guest" ? "Starting guest session…" : "Try the demo as a guest"}
          <ArrowRight className="size-4 transition-transform group-hover:translate-x-0.5 motion-reduce:transition-none" aria-hidden="true" />
        </button>
      </p>
    </div>
  );
}
