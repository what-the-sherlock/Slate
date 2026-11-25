"use client";

import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Check, KeyRound, Lock, Mail, User, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { PasswordToggle } from "@/components/password-toggle";
import { Turnstile } from "@/components/turnstile";
import { Spinner } from "@/components/ui/button";
import { api, ApiProblem, errorMessage, unwrap } from "@/lib/api/client";
import { keys, useAuthConfig } from "@/lib/api/hooks";
import { EMAIL, PASSWORD_RULES, passwordOk } from "@/lib/password";
import { cn } from "@/lib/utils";
import { heroSurface } from "../auth-frame";

type Field = "name" | "email" | "password" | "setup_token";

type FieldSpec = {
  id: Field;
  label: string;
  type: string;
  autoComplete: string;
  placeholder: string;
  icon: LucideIcon;
  hint?: string;
};

const FIELDS: FieldSpec[] = [
  { id: "name", label: "What's your name?", type: "text", autoComplete: "name", placeholder: "Ada Lovelace", icon: User },
  { id: "email", label: "Your email", type: "email", autoComplete: "username", placeholder: "name@company.com", icon: Mail, hint: "We'll send a 6-digit code to verify it." },
  { id: "password", label: "Create a password", type: "password", autoComplete: "new-password", placeholder: "At least 12 characters", icon: Lock },
];

// Only shown when the server says this address is the owner's (OWNER_EMAIL needs SETUP_TOKEN).
const TOKEN_FIELD: FieldSpec = {
  id: "setup_token",
  label: "Enter the setup code",
  type: "password",
  autoComplete: "off",
  placeholder: "Setup code",
  icon: KeyRound,
  hint: "This address is reserved for the owner. Enter the server's SETUP_TOKEN.",
};

/** The message that stops a step, or null when it may move on. The server stays the authority. */
export function stepProblem(field: Field, value: string): string | null {
  switch (field) {
    case "name":
      return value.trim() ? null : "Enter your name.";
    case "email":
      return EMAIL.test(value.trim()) ? null : "Enter an email like name@company.com.";
    case "password":
      return passwordOk(value) ? null : "Your password doesn't meet the rules yet.";
    case "setup_token":
      return value.trim() ? null : "Enter the setup code.";
  }
}

/**
 * Sign-up as steps in the hero's pill bar (UI_DESIGN §4.1, ADR-0021): one question at a time,
 * Enter to continue. It's one <form> throughout, and answered fields stay mounted (hidden, not
 * removed) with their autocomplete hints, so browser autofill and password managers still work.
 * A server field error jumps back to the step that owns it. Bot defences: the Turnstile widget
 * (usually invisible) and a honeypot field people never see.
 */
export function SignUpForm() {
  const router = useRouter();
  const client = useQueryClient();
  const config = useAuthConfig();
  const [values, setValues] = useState<Record<Field, string>>({ name: "", email: "", password: "", setup_token: "" });
  const [needsToken, setNeedsToken] = useState(false);
  const [step, setStep] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [human, setHuman] = useState<string | null>(null);
  const resetHuman = useRef<() => void>(() => {});
  const trap = useRef<HTMLInputElement>(null);
  const inputs = useRef<Partial<Record<Field, HTMLInputElement | null>>>({});

  const fields = needsToken ? [...FIELDS, TOKEN_FIELD] : FIELDS;
  const index = Math.min(step, fields.length - 1);
  const current = fields[index] ?? FIELDS[0]!;
  const last = index === fields.length - 1;
  const ready = stepProblem(current.id, values[current.id]) === null;
  const rulesMet = PASSWORD_RULES.map((r) => r.test(values.password));
  const closed = config.data?.signups_enabled === false;

  useEffect(() => {
    inputs.current[current.id]?.focus();
  }, [current.id]);

  function go(next: number) {
    setError(null);
    setStep(Math.max(0, Math.min(next, fields.length - 1)));
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const problem = stepProblem(current.id, values[current.id]);
    if (problem) return setError(problem);
    if (!last) return go(index + 1);
    if (!human) return setError("Just a moment: we're checking you're not a bot.");

    setBusy(true);
    setError(null);
    try {
      unwrap(
        await api.POST("/api/v1/auth/signup", {
          body: {
            name: values.name.trim(),
            email: values.email.trim(),
            password: values.password,
            turnstile_token: human,
            setup_token: values.setup_token || null,
            website: trap.current?.value ?? "",
          },
        }),
      );
      await client.invalidateQueries({ queryKey: keys.me });
      router.replace("/verify");
    } catch (err) {
      resetHuman.current(); // the token was used, whatever the outcome
      if (err instanceof ApiProblem && err.fields.setup_token && !needsToken) {
        setNeedsToken(true);
        setStep(FIELDS.length);
        setError(null);
      } else {
        const list = needsToken ? [...FIELDS, TOKEN_FIELD] : FIELDS;
        const owner = err instanceof ApiProblem ? list.findIndex((f) => err.fields[f.id]) : -1;
        const field = list[owner];
        if (err instanceof ApiProblem && field) {
          setStep(owner);
          setError(err.fields[field.id] ?? errorMessage(err));
        } else {
          setError(errorMessage(err));
        }
      }
      setBusy(false);
    }
  }

  if (closed) {
    return (
      <p role="status" className="rounded-full bg-bg px-4 py-2 text-body font-medium text-fg">
        Sign-ups are closed right now.{" "}
        <Link href="/sign-in" className="font-semibold text-brand underline-offset-[3px] hover:underline">
          Sign in
        </Link>
      </p>
    );
  }

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col items-center gap-3">
      <p className="font-mono text-body text-white">
        Step {index + 1} of {fields.length}
      </p>

      {/* Honeypot: off-screen, unfocusable and unlabelled for assistive tech. People never fill it. */}
      <div className="absolute -left-[9999px] h-px w-px overflow-hidden" aria-hidden="true">
        <label htmlFor="website">Website</label>
        <input ref={trap} id="website" name="website" type="text" tabIndex={-1} autoComplete="off" defaultValue="" />
      </div>

      <div
        className={cn(
          "flex w-full items-center gap-2 rounded-2xl p-1.5 pl-4 text-left transition-shadow duration-300 focus-within:ring-4 focus-within:ring-white/45",
          heroSurface,
        )}
      >
        {fields.map((f, i) => {
          const Icon = f.icon;
          const active = i === index;
          return (
            <div key={f.id} className={active ? "flex min-w-0 flex-1 items-center gap-2" : "sr-only"} aria-hidden={active ? undefined : true}>
              <label htmlFor={f.id} className="sr-only">
                {f.label}
              </label>
              <Icon className="size-5 shrink-0 text-fg-muted" strokeWidth={1.5} aria-hidden="true" />
              <input
                ref={(el) => {
                  inputs.current[f.id] = el;
                }}
                id={f.id}
                name={f.id}
                type={f.id === "password" && showPassword ? "text" : f.type}
                autoComplete={f.autoComplete}
                placeholder={f.placeholder}
                tabIndex={active ? undefined : -1}
                value={values[f.id]}
                onChange={(e) => {
                  setValues((v) => ({ ...v, [f.id]: e.target.value }));
                  if (active) setError(null);
                }}
                aria-invalid={active && error ? true : undefined}
                aria-describedby={active ? (error ? "signup-error" : f.id === "password" ? "password-rules" : undefined) : undefined}
                className="h-11 min-w-0 flex-1 bg-transparent text-[16px] text-fg outline-none placeholder:text-fg-subtle focus-visible:outline-none"
              />
              {f.id === "password" && active ? (
                <PasswordToggle shown={showPassword} onToggle={() => setShowPassword((s) => !s)} controls="password" className="size-9" />
              ) : null}
            </div>
          );
        })}
        <button
          type="submit"
          disabled={busy}
          data-state={ready ? "ready" : "idle"}
          className={cn(
            "group flex h-11 shrink-0 items-center gap-2 rounded-xl px-4 text-ui font-semibold transition-all duration-300 sm:px-5",
            ready
              ? "bg-brand text-white shadow-[0_6px_16px_-8px_rgb(48_80_240/0.9)] hover:bg-brand-strong active:scale-[0.98]"
              : "bg-brand-subtle text-brand",
          )}
        >
          {busy ? <Spinner className="size-4" /> : null}
          <span className="sr-only sm:not-sr-only">{last ? (busy ? "Creating…" : "Create account") : "Continue"}</span>
          {busy ? null : (
            <ArrowRight
              className="size-4 transition-transform duration-300 group-hover:translate-x-0.5 motion-reduce:transition-none"
              strokeWidth={2.25}
              aria-hidden="true"
            />
          )}
        </button>
      </div>

      <div className="flex min-h-7 items-center justify-center">
        {error ? (
          <p
            id="signup-error"
            role="alert"
            className="animate-in rounded-full bg-bg px-3 py-1 text-body font-medium text-danger duration-300 fade-in slide-in-from-top-1"
          >
            {error}
          </p>
        ) : current.id === "password" ? (
          <ul id="password-rules" className="flex flex-wrap justify-center gap-x-5 gap-y-1.5">
            {PASSWORD_RULES.map((rule, i) => (
              <li key={rule.label} className="flex items-center gap-1.5 text-body text-white">
                {rulesMet[i] ? (
                  <Check className="size-3.5 animate-in zoom-in-50" strokeWidth={2.5} aria-hidden="true" />
                ) : (
                  <span className="size-3 rounded-full border border-white" aria-hidden="true" />
                )}
                {rule.label}
                <span className="sr-only">{rulesMet[i] ? "(met)" : "(not met yet)"}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-body text-white">{current.hint ?? current.label}</p>
        )}
      </div>

      {/* Mounted from the start so the (usually invisible) check is done by the last step. */}
      {config.data ? (
        <Turnstile
          siteKey={config.data.turnstile_site_key}
          action="signup"
          onToken={setHuman}
          onError={setError}
          onReady={(reset) => {
            resetHuman.current = reset;
          }}
        />
      ) : null}

      <div className="flex h-5 items-center gap-4">
        {index > 0 ? (
          <button
            type="button"
            onClick={() => go(index - 1)}
            className="inline-flex items-center gap-1 text-body text-white underline-offset-[3px] hover:underline"
          >
            <ArrowLeft className="size-3.5" aria-hidden="true" />
            Back
          </button>
        ) : null}
        <span className="flex gap-1.5" aria-hidden="true">
          {fields.map((f, i) => (
            <span
              key={f.id}
              className={cn("h-1.5 rounded-full bg-white transition-all duration-300", i === index ? "w-5" : "w-1.5", i > index && "opacity-50")}
            />
          ))}
        </span>
      </div>
    </form>
  );
}
