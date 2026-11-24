"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { api, ApiProblem, errorMessage, unwrap } from "@/lib/api/client";
import { useSetupStatus } from "@/lib/api/hooks";

type Field = "workspace_name" | "name" | "email" | "password" | "setup_token";

const FIELDS: { id: Field; label: string; type: string; autoComplete: string; hint?: string }[] = [
  { id: "workspace_name", label: "Workspace name", type: "text", autoComplete: "organization" },
  { id: "name", label: "Your name", type: "text", autoComplete: "name" },
  { id: "email", label: "Email", type: "email", autoComplete: "username" },
  { id: "password", label: "Password", type: "password", autoComplete: "new-password", hint: "At least 12 characters." },
];

export function SetupForm() {
  const router = useRouter();
  const client = useQueryClient();
  const status = useSetupStatus();
  const [values, setValues] = useState<Record<Field, string>>({
    workspace_name: "",
    name: "",
    email: "",
    password: "",
    setup_token: "",
  });
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<Field, string>>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (status.data?.set_up) router.replace("/sign-in");
  }, [status.data, router]);

  const fields = status.data?.requires_setup_token
    ? [
        ...FIELDS,
        {
          id: "setup_token" as const,
          label: "Setup code",
          type: "password",
          autoComplete: "off",
          hint: "The SETUP_TOKEN configured on the server.",
        },
      ]
    : FIELDS;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setFieldErrors({});
    try {
      unwrap(
        await api.POST("/api/v1/auth/setup", {
          body: { ...values, setup_token: values.setup_token || null },
        }),
      );
      await client.invalidateQueries();
      router.replace("/sources");
    } catch (err) {
      if (err instanceof ApiProblem && Object.keys(err.fields).length) {
        setFieldErrors(err.fields as Partial<Record<Field, string>>);
      } else {
        setError(errorMessage(err));
      }
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} noValidate className="rounded-lg border border-brand-border bg-bg p-6 text-left shadow-sm">
      <div className="flex flex-col gap-4">
        {fields.map((f) => {
          const err = fieldErrors[f.id];
          const describedBy = err ? `${f.id}-error` : f.hint ? `${f.id}-hint` : undefined;
          return (
            <div key={f.id} className="flex flex-col gap-1.5">
              <label htmlFor={f.id} className="text-sm font-medium text-fg">
                {f.label}
              </label>
              <input
                id={f.id}
                name={f.id}
                type={f.type}
                autoComplete={f.autoComplete}
                value={values[f.id]}
                onChange={(e) => setValues((v) => ({ ...v, [f.id]: e.target.value }))}
                aria-invalid={err ? true : undefined}
                aria-describedby={describedBy}
                className="h-10 rounded-md border border-[var(--sl-border-input)] bg-bg px-3 text-base text-fg outline-none transition-colors focus:border-brand focus:shadow-[0_0_0_3px_var(--sl-accent-subtle)] aria-invalid:border-danger sm:text-sm"
              />
              {err ? (
                <p id={`${f.id}-error`} className="text-[13px] text-danger">
                  {err}
                </p>
              ) : f.hint ? (
                <p id={`${f.id}-hint`} className="text-[13px] text-fg-subtle">
                  {f.hint}
                </p>
              ) : null}
            </div>
          );
        })}
      </div>
      {error ? (
        <p role="alert" className="mt-4 rounded-md border border-danger-border bg-danger-subtle px-3 py-2 text-[13px] text-danger">
          {error}
        </p>
      ) : null}
      <Button type="submit" variant="primary" size="lg" loading={busy} className="mt-6 w-full">
        Create workspace
      </Button>
    </form>
  );
}
