"use client";

import { Check, Hash, Lock, Mail } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useRef, useState, type FormEvent } from "react";
import { PasswordToggle } from "@/components/password-toggle";
import { Turnstile } from "@/components/turnstile";
import { Button } from "@/components/ui/button";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { useAuthConfig } from "@/lib/api/hooks";
import { CODE, EMAIL, normaliseCode, PASSWORD_RULES, passwordOk } from "@/lib/password";
import { cn } from "@/lib/utils";
import { fieldIcon, fieldInput, fieldLabel, fieldShell, heroSurface } from "../auth-frame";

/**
 * Password reset by emailed code (ADR-0021). The request step always answers the same way,
 * whether or not the address has an account, so this page can't be used to find accounts.
 */
export function ForgotPasswordForm() {
  const router = useRouter();
  const params = useSearchParams();
  const config = useAuthConfig();
  const [step, setStep] = useState<"email" | "reset">("email");
  const [email, setEmail] = useState(params.get("email") ?? "");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [human, setHuman] = useState<string | null>(null);
  const resetHuman = useRef<() => void>(() => {});

  async function requestCode(e: FormEvent) {
    e.preventDefault();
    setError(null);
    if (!EMAIL.test(email.trim())) return setError("Enter an email like name@company.com.");
    if (!human) return setError("Just a moment: we're checking you're not a bot.");
    setBusy(true);
    try {
      unwrap(await api.POST("/api/v1/auth/password/forgot", { body: { email: email.trim(), turnstile_token: human } }));
      setStep("reset");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      resetHuman.current();
      setBusy(false);
    }
  }

  async function reset(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const clean = normaliseCode(code);
    if (!CODE.test(clean)) return setError("Enter the 6-digit code from the email.");
    if (!passwordOk(password)) return setError("Your new password doesn't meet the rules yet.");
    setBusy(true);
    try {
      unwrap(await api.POST("/api/v1/auth/password/reset", { body: { email: email.trim(), code: clean, password } }));
      router.replace("/sign-in?reason=reset");
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col items-center gap-3">
      <form onSubmit={step === "email" ? requestCode : reset} noValidate className={cn("flex w-full flex-col gap-4 rounded-2xl p-5 text-left sm:p-6", heroSurface)}>
        {step === "email" ? (
          <div className="flex flex-col gap-2">
            <label htmlFor="email" className={fieldLabel}>
              Your account&apos;s email
            </label>
            <div className={fieldShell}>
              <Mail className={fieldIcon} strokeWidth={1.5} aria-hidden="true" />
              <input
                id="email"
                type="email"
                autoComplete="username"
                autoFocus
                placeholder="you@company.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className={fieldInput}
              />
            </div>
          </div>
        ) : (
          <>
            <p role="status" className="text-caption text-fg-muted">
              If <span className="font-medium text-fg">{email.trim()}</span> has an account, we sent it a code. It expires in 15
              minutes.
            </p>
            <div className="flex flex-col gap-2">
              <label htmlFor="code" className={fieldLabel}>
                Code
              </label>
              <div className={fieldShell}>
                <Hash className={fieldIcon} strokeWidth={1.5} aria-hidden="true" />
                <input
                  id="code"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  autoFocus
                  maxLength={12}
                  placeholder="6-digit code"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  className={cn(fieldInput, "font-mono tracking-[0.2em]")}
                />
              </div>
            </div>
            <div className="flex flex-col gap-2">
              <label htmlFor="new-password" className={fieldLabel}>
                New password
              </label>
              <div className={cn(fieldShell, "pr-1")}>
                <Lock className={fieldIcon} strokeWidth={1.5} aria-hidden="true" />
                <input
                  id="new-password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="new-password"
                  placeholder="At least 12 characters"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  aria-describedby="reset-rules"
                  className={fieldInput}
                />
                <PasswordToggle shown={showPassword} onToggle={() => setShowPassword((s) => !s)} controls="new-password" />
              </div>
              <ul id="reset-rules" className="flex flex-wrap gap-x-4 gap-y-1">
                {PASSWORD_RULES.map((rule) => {
                  const met = rule.test(password);
                  return (
                    <li key={rule.label} className={cn("flex items-center gap-1.5 text-caption", met ? "text-fg" : "text-fg-subtle")}>
                      {met ? <Check className="size-3.5" aria-hidden="true" /> : <span className="size-3 rounded-full border border-border-strong" aria-hidden="true" />}
                      {rule.label}
                      <span className="sr-only">{met ? "(met)" : "(not met yet)"}</span>
                    </li>
                  );
                })}
              </ul>
            </div>
          </>
        )}
        {error ? (
          <p role="alert" className="-mt-1 text-caption text-danger">
            {error}
          </p>
        ) : null}
        <Button type="submit" variant="primary" size="lg" className="w-full" loading={busy}>
          {step === "email" ? "Email me a code" : "Set new password"}
        </Button>
        {step === "reset" ? (
          <button
            type="button"
            className="text-caption font-medium text-brand underline-offset-[3px] hover:underline"
            onClick={() => {
              setStep("email");
              setError(null);
            }}
          >
            Didn&apos;t get it? Send another code
          </button>
        ) : null}
      </form>
      {config.data && step === "email" ? (
        <Turnstile
          siteKey={config.data.turnstile_site_key}
          action="reset"
          onToken={setHuman}
          onError={setError}
          onReady={(r) => {
            resetHuman.current = r;
          }}
        />
      ) : null}
    </div>
  );
}
