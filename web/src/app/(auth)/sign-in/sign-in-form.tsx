"use client";

import { useQueryClient } from "@tanstack/react-query";
import { cn } from "@/lib/utils";
import { ArrowRight, Lock, Mail } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { api, ApiProblem, errorMessage, unwrap } from "@/lib/api/client";
import { keys } from "@/lib/api/hooks";
import { PasswordToggle } from "@/components/password-toggle";
import { EMAIL } from "@/lib/password";
import { safeNext } from "@/lib/safe-next";
import { fieldIcon, fieldInput, fieldLabel, fieldShell, heroSurface, TrustRow } from "../auth-frame";

/** After this long, say the server is waking up (Neon scales to zero; a cold start takes seconds). */
export const SLOW_AFTER_MS = 3_000;
/** Give up after this long so a stalled request never leaves the form stuck. */
export const SIGN_IN_TIMEOUT_MS = 20_000;
const TIMED_OUT = "The server took too long to respond. Try again.";

class TimedOut extends Error {}

/**
 * Run a request with a deadline, calling onSlow once it passes SLOW_AFTER_MS. Not retried
 * automatically: each sign-in attempt counts against the rate limit, so the user retries.
 */
async function withDeadline<T>(run: (signal: AbortSignal) => Promise<T>, onSlow: () => void): Promise<T> {
  const controller = new AbortController();
  const slow = setTimeout(onSlow, SLOW_AFTER_MS);
  const deadline = setTimeout(() => controller.abort(), SIGN_IN_TIMEOUT_MS);
  try {
    return await run(controller.signal);
  } catch (err) {
    throw controller.signal.aborted ? new TimedOut(TIMED_OUT) : err;
  } finally {
    clearTimeout(slow);
    clearTimeout(deadline);
  }
}

/**
 * Two-step sign-in in one <form> (UI_DESIGN §4.1): the hero's email bar grows into a password
 * card. The email input stays mounted (autocomplete="username") so password managers work.
 * Continue is tinted until the email looks valid, then solid; it still submits either way,
 * so an invalid address gets a message instead of a dead button.
 */
export function SignInForm() {
  const router = useRouter();
  const params = useSearchParams();
  const client = useQueryClient();
  const [step, setStep] = useState<"email" | "password">("email");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [slow, setSlow] = useState(false);
  const passwordRef = useRef<HTMLInputElement>(null);
  const next = safeNext(params.get("next"), typeof window === "undefined" ? "" : window.location.origin);

  useEffect(() => {
    if (step !== "password") return;
    passwordRef.current?.focus();
    // Load the destination while the user types, so the redirect after sign-in is instant.
    router.prefetch(next);
  }, [step, router, next]);

  const reason = params.get("reason");
  const banner =
    reason === "expired"
      ? "Your session expired. Sign in again to continue."
      : reason === "reset"
        ? "Password changed. Sign in with your new password."
        : null;
  const markSlow = () => setSlow(true);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (step === "email") {
      if (!EMAIL.test(email.trim())) return setError("Enter an email like name@company.com.");
      return setStep("password");
    }
    setBusy(true);
    try {
      const me = await withDeadline(
        async (signal) => unwrap(await api.POST("/api/v1/auth/login", { body: { email: email.trim(), password }, signal })),
        markSlow,
      );
      client.setQueryData(keys.me, me);
      // An unverified account can sign in, but can't use the app until it verifies (ADR-0021).
      router.replace(me.email_verified ? next : "/verify");
    } catch (err) {
      setError(err instanceof ApiProblem && err.status === 401 ? "Email or password is incorrect." : errorMessage(err));
      setPassword("");
      setBusy(false);
    } finally {
      setSlow(false);
    }
  }

  const emailOk = EMAIL.test(email.trim());

  return (
    <div className="flex flex-col items-center gap-4">
      {banner ? (
        <p role="status" className="rounded-full bg-bg px-3 py-1 text-body font-medium text-brand">
          {banner}
        </p>
      ) : null}
      <form
        onSubmit={onSubmit}
        noValidate
        className={cn(
          "w-full rounded-2xl text-left transition-shadow duration-300",
          heroSurface,
          step === "email" ? "p-1.5 pl-4 focus-within:ring-4 focus-within:ring-white/45" : "p-5 sm:p-6",
        )}
      >
        <label htmlFor="email" className="sr-only">
          Email
        </label>
        <div className={step === "email" ? "flex items-center gap-2" : "sr-only"}>
          <Mail className="size-5 shrink-0 text-fg-muted" strokeWidth={1.5} aria-hidden="true" />
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
            className="h-11 min-w-0 flex-1 bg-transparent text-[16px] text-fg outline-none placeholder:text-fg-subtle focus-visible:outline-none"
          />
          <button
            type="submit"
            data-state={emailOk ? "ready" : "idle"}
            className={cn(
              "group flex h-11 shrink-0 items-center gap-2 rounded-xl px-4 text-ui font-semibold transition-all duration-300 sm:px-5",
              emailOk
                ? "bg-brand text-white shadow-[0_6px_16px_-8px_rgb(48_80_240/0.9)] hover:bg-brand-strong active:scale-[0.98]"
                : "bg-brand-subtle text-brand",
            )}
          >
            <span className="sr-only sm:not-sr-only">Continue</span>
            <ArrowRight
              className="size-4 transition-transform duration-300 group-hover:translate-x-0.5 motion-reduce:transition-none"
              strokeWidth={2.25}
              aria-hidden="true"
            />
          </button>
        </div>

        {step === "password" ? (
          <div className="flex animate-in flex-col gap-4 duration-300 ease-out fade-in zoom-in-[0.98] motion-reduce:animate-none">
            <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-subtle px-3 py-2 text-caption">
              <span className="flex min-w-0 items-center gap-2 text-fg">
                <Mail className={fieldIcon} strokeWidth={1.5} aria-hidden="true" />
                <span className="truncate">{email}</span>
              </span>
              <button
                type="button"
                className="shrink-0 font-medium text-brand underline-offset-[3px] hover:underline"
                onClick={() => {
                  setStep("email");
                  setPassword("");
                  setError(null);
                }}
              >
                Change
              </button>
            </div>
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <label htmlFor="password" className={fieldLabel}>
                  Password
                </label>
                <Link
                  href={`/forgot-password?email=${encodeURIComponent(email.trim())}`}
                  className="text-caption font-medium text-brand underline-offset-[3px] hover:underline"
                >
                  Forgot password?
                </Link>
              </div>
              <div className={cn(fieldShell, "pr-1")}>
                <Lock className={fieldIcon} strokeWidth={1.5} aria-hidden="true" />
                <input
                  ref={passwordRef}
                  id="password"
                  type={showPassword ? "text" : "password"}
                  name="password"
                  autoComplete="current-password"
                  placeholder="Your password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  aria-invalid={error ? true : undefined}
                  aria-describedby={error ? "signin-error" : undefined}
                  className={fieldInput}
                />
                <PasswordToggle shown={showPassword} onToggle={() => setShowPassword((s) => !s)} controls="password" />
              </div>
            </div>
            {error ? (
              <p id="signin-error" role="alert" className="-mt-1 text-caption text-danger">
                {error}
              </p>
            ) : null}
            <Button type="submit" variant="primary" size="lg" className="w-full" loading={busy} disabled={!password}>
              Sign in
            </Button>
          </div>
        ) : null}
      </form>

      <div className="flex min-h-7 items-center justify-center">
        {error && step === "email" ? (
          <p
            id="signin-error"
            role="alert"
            className="animate-in rounded-full bg-bg px-3 py-1 text-body font-medium text-danger duration-300 fade-in slide-in-from-top-1"
          >
            {error}
          </p>
        ) : slow ? (
          <p role="status" className="rounded-full bg-bg px-3 py-1 text-body font-medium text-fg-muted">
            Waking up the server. This can take a few seconds…
          </p>
        ) : (
          <TrustRow items={["Cited answers", "Permission-aware", "Your own private workspace"]} />
        )}
      </div>

      <p className="text-body text-white">
        No account?{" "}
        <Link
          href="/sign-up"
          className="group inline-flex items-center gap-1 font-semibold text-white underline decoration-white/50 underline-offset-[3px] hover:decoration-white"
        >
          Create one free
          <ArrowRight className="size-4 transition-transform group-hover:translate-x-0.5 motion-reduce:transition-none" aria-hidden="true" />
        </Link>
      </p>
    </div>
  );
}
