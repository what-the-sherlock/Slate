"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Hash } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Turnstile } from "@/components/turnstile";
import { Spinner } from "@/components/ui/button";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { keys, useAuthConfig, useMe, useSignOut } from "@/lib/api/hooks";
import { CODE, normaliseCode } from "@/lib/password";
import { cn } from "@/lib/utils";
import { heroSurface } from "../auth-frame";

/**
 * Email verification (ADR-0021). Until this succeeds the account can sign in but can't
 * upload, search or chat. The code field uses autocomplete="one-time-code" so phones offer
 * the code from the email. Resend needs a fresh bot check (it sends mail).
 */
export function VerifyForm() {
  const router = useRouter();
  const client = useQueryClient();
  const me = useMe();
  const config = useAuthConfig();
  const signOut = useSignOut();
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<"verify" | "resend" | null>(null);
  const [human, setHuman] = useState<string | null>(null);
  const resetHuman = useRef<() => void>(() => {});
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (me.data === null) router.replace("/sign-in?next=/verify");
    else if (me.data?.email_verified) router.replace("/home");
  }, [me.data, router]);

  useEffect(() => input.current?.focus(), []);

  const clean = normaliseCode(code);
  const ready = CODE.test(clean);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setNotice(null);
    if (!ready) return setError("Enter the 6-digit code from the email.");
    setBusy("verify");
    setError(null);
    try {
      const updated = unwrap(await api.POST("/api/v1/auth/verify", { body: { code: clean } }));
      client.setQueryData(keys.me, updated);
      router.replace("/home");
    } catch (err) {
      setError(errorMessage(err));
      setCode("");
      input.current?.focus();
      setBusy(null);
    }
  }

  async function resend() {
    setError(null);
    setNotice(null);
    if (!human) return setError("Just a moment: we're checking you're not a bot.");
    setBusy("resend");
    try {
      unwrap(await api.POST("/api/v1/auth/verify/resend", { body: { turnstile_token: human } }));
      setNotice("We sent a new code. The previous one no longer works.");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      resetHuman.current();
      setBusy(null);
    }
  }

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col items-center gap-3">
      {me.data ? (
        <p className="text-body text-white">
          Sent to <span className="font-semibold">{me.data.user.email}</span>
        </p>
      ) : null}
      <div
        className={cn(
          "flex w-full items-center gap-2 rounded-2xl p-1.5 pl-4 text-left transition-shadow duration-300 focus-within:ring-4 focus-within:ring-white/45",
          heroSurface,
        )}
      >
        <label htmlFor="code" className="sr-only">
          Verification code
        </label>
        <Hash className="size-5 shrink-0 text-fg-muted" strokeWidth={1.5} aria-hidden="true" />
        <input
          ref={input}
          id="code"
          name="code"
          inputMode="numeric"
          autoComplete="one-time-code"
          maxLength={12}
          placeholder="6-digit code"
          value={code}
          onChange={(e) => {
            setCode(e.target.value);
            setError(null);
          }}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? "verify-error" : undefined}
          className="h-11 min-w-0 flex-1 bg-transparent font-mono text-[18px] tracking-[0.3em] text-fg outline-none placeholder:font-sans placeholder:text-[16px] placeholder:tracking-normal placeholder:text-fg-subtle focus-visible:outline-none"
        />
        <button
          type="submit"
          disabled={busy !== null}
          data-state={ready ? "ready" : "idle"}
          className={cn(
            "group flex h-11 shrink-0 items-center gap-2 rounded-xl px-4 text-ui font-semibold transition-all duration-300 sm:px-5",
            ready
              ? "bg-brand text-white shadow-[0_6px_16px_-8px_rgb(48_80_240/0.9)] hover:bg-brand-strong active:scale-[0.98]"
              : "bg-brand-subtle text-brand",
          )}
        >
          {busy === "verify" ? <Spinner className="size-4" /> : null}
          <span className="sr-only sm:not-sr-only">{busy === "verify" ? "Checking…" : "Verify"}</span>
          {busy === "verify" ? null : <ArrowRight className="size-4" strokeWidth={2.25} aria-hidden="true" />}
        </button>
      </div>

      <div className="flex min-h-7 items-center justify-center">
        {error ? (
          <p id="verify-error" role="alert" className="rounded-full bg-bg px-3 py-1 text-body font-medium text-danger">
            {error}
          </p>
        ) : notice ? (
          <p role="status" className="rounded-full bg-bg px-3 py-1 text-body font-medium text-fg">
            {notice}
          </p>
        ) : (
          <p className="text-body text-white">The code expires 15 minutes after it was sent.</p>
        )}
      </div>

      {config.data ? (
        <Turnstile
          siteKey={config.data.turnstile_site_key}
          action="resend"
          onToken={setHuman}
          onError={setError}
          onReady={(reset) => {
            resetHuman.current = reset;
          }}
        />
      ) : null}

      <p className="flex flex-wrap items-center justify-center gap-x-4 gap-y-1 text-body text-white">
        <button
          type="button"
          onClick={resend}
          disabled={busy !== null}
          className="inline-flex items-center gap-1.5 font-semibold underline decoration-white/50 underline-offset-[3px] hover:decoration-white disabled:opacity-60"
        >
          {busy === "resend" ? <Spinner className="size-3.5" /> : null}
          Send a new code
        </button>
        <button
          type="button"
          onClick={() => signOut.mutate(undefined, { onSettled: () => router.replace("/sign-in") })}
          className="underline decoration-white/50 underline-offset-[3px] hover:decoration-white"
        >
          Use a different account
        </button>
      </p>
    </form>
  );
}
