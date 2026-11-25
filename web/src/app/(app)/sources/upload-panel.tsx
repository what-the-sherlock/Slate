"use client";

import { useQueryClient } from "@tanstack/react-query";
import { cn } from "@/lib/utils";
import { ArrowLeft, Check, CircleAlert, FileText, UploadCloud } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { toast } from "sonner";
import { Button, Spinner } from "@/components/ui/button";
import { api, errorMessage, unwrap } from "@/lib/api/client";
import { keys } from "@/lib/api/hooks";
import { formatBytes } from "@/lib/format";
import { ACCEPTED, precheck, uploadFile, type UploadPhase, type Visibility } from "@/lib/upload";

type Item = { id: string; file: File; phase: UploadPhase };

const VISIBILITIES: { value: Visibility; label: string; hint: string; adminOnly?: boolean }[] = [
  { value: "only_me", label: "Only me", hint: "Private to you" },
  { value: "workspace", label: "Workspace", hint: "Everyone in the workspace" },
  { value: "public", label: "Public", hint: "Can also go to free AI providers", adminOnly: true },
];

/** Dropzone + per-file progress (UI_DESIGN §3.2, §4.5). */
export function UploadPanel({ canPublish, onBack }: { canPublish: boolean; onBack: () => void }) {
  const client = useQueryClient();
  const [items, setItems] = useState<Item[]>([]);
  const [visibility, setVisibility] = useState<Visibility>("only_me");
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const update = useCallback((id: string, phase: UploadPhase) => {
    setItems((all) => all.map((it) => (it.id === id ? { ...it, phase } : it)));
  }, []);

  async function start(files: FileList | File[]) {
    const added = Array.from(files).map((file) => ({
      id: crypto.randomUUID(),
      file,
      phase: { kind: "hashing" } as UploadPhase,
    }));
    setItems((all) => [...added, ...all]);
    for (const item of added) {
      try {
        await uploadFile(item.file, visibility, (phase) => update(item.id, phase));
      } catch (err) {
        update(item.id, { kind: "failed", message: errorMessage(err) });
      }
    }
    void client.invalidateQueries({ queryKey: keys.sources });
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer.files.length) void start(e.dataTransfer.files);
  }

  return (
    <div className="flex flex-col gap-5">
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-2 text-body font-medium text-fg">Who can see these files?</legend>
        <div className="grid gap-2 sm:grid-cols-3">
          {VISIBILITIES.filter((v) => !v.adminOnly || canPublish).map((v) => (
            <label
              key={v.value}
              className={cn(
                "flex cursor-pointer flex-col rounded-md border px-3 py-2.5 transition-colors",
                visibility === v.value ? "border-brand bg-brand-subtle shadow-[0_0_0_3px_var(--sl-accent-subtle)]" : "border-border hover:border-border-strong hover:bg-subtle",
              )}
            >
              <input
                type="radio"
                name="visibility"
                value={v.value}
                checked={visibility === v.value}
                onChange={() => setVisibility(v.value)}
                className="sr-only"
              />
              <span className="text-body font-medium text-fg">{v.label}</span>
              <span className="text-data text-fg-subtle">{v.hint}</span>
            </label>
          ))}
        </div>
      </fieldset>

      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={cn(
          "flex flex-col items-center gap-2 rounded-lg border-[1.5px] border-dashed px-6 py-8 text-center transition-colors",
          dragging ? "border-brand bg-brand-subtle" : "border-border-strong bg-subtle",
        )}
      >
        <UploadCloud
          className={cn("size-6 text-fg-muted transition-transform motion-reduce:transition-none", dragging && "-translate-y-0.5 text-brand")}
          strokeWidth={1.6}
          aria-hidden="true"
        />
        <p className="text-body text-fg">
          Drop files here or{" "}
          <button type="button" className="font-medium text-brand underline-offset-[3px] hover:underline" onClick={() => input.current?.click()}>
            browse
          </button>
        </p>
        <p className="text-data text-fg-subtle">PDF, Word, Markdown, HTML, CSV or text · up to 25 MB each</p>
        <input
          ref={input}
          type="file"
          multiple
          accept={ACCEPTED}
          className="sr-only"
          aria-label="Choose files to upload"
          onChange={(e) => {
            if (e.target.files?.length) void start(e.target.files);
            e.target.value = "";
          }}
        />
      </div>

      {items.length ? (
        <ul className="flex max-h-64 flex-col gap-1 overflow-y-auto" aria-live="polite">
          {items.map((it) => (
            <FileRow key={it.id} item={it} onPhase={(p) => update(it.id, p)} />
          ))}
        </ul>
      ) : null}

      <div className="flex justify-start">
        <Button variant="ghost" size="sm" onClick={onBack}>
          <ArrowLeft className="size-3.5" aria-hidden="true" />
          All sources
        </Button>
      </div>
    </div>
  );
}

function FileRow({ item, onPhase }: { item: Item; onPhase: (p: UploadPhase) => void }) {
  const { phase } = item;
  const jobId = phase.kind === "processing" ? phase.jobId : null;

  // Poll a queued job only while this row is visible and the job is unfinished.
  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const job = unwrap(await api.GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: jobId } } }));
        if (cancelled) return;
        if (job.status === "succeeded") {
          onPhase({ kind: "ready" });
          toast.success(`${item.file.name} is ready`);
        } else if (job.status === "dead") onPhase({ kind: "failed", message: job.error ?? "Processing failed" });
        else timer = setTimeout(tick, 5000);
      } catch {
        if (!cancelled) timer = setTimeout(tick, 10000);
      }
    };
    let timer = setTimeout(tick, 3000);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [jobId, item.file.name, onPhase]);

  const preError = precheck(item.file);
  const failure = phase.kind === "failed" ? (preError ?? phase.message) : null;
  return (
    <li className="flex items-center gap-3 rounded-md px-2 py-2 hover:bg-subtle">
      <FileText className="size-4 shrink-0 text-fg-subtle" aria-hidden="true" />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-body text-fg" title={item.file.name}>
          {item.file.name}
        </span>
        <span className="block text-data text-fg-subtle tabular-nums">{formatBytes(item.file.size)}</span>
        {phase.kind === "uploading" ? (
          <span className="mt-1.5 block h-1 overflow-hidden rounded-full bg-emphasis" aria-hidden="true">
            <span className="block h-full rounded-full bg-brand transition-[width]" style={{ width: `${Math.round(phase.progress * 100)}%` }} />
          </span>
        ) : null}
        {failure ? <span className="mt-0.5 block text-data text-danger">{failure}</span> : null}
      </span>
      <PhaseLabel phase={phase} />
    </li>
  );
}

function PhaseLabel({ phase }: { phase: UploadPhase }) {
  const base = "inline-flex shrink-0 items-center gap-1.5 text-caption";
  switch (phase.kind) {
    case "hashing":
      return <span className={cn(base, "text-fg-subtle")}><Spinner className="size-3" />Preparing</span>;
    case "uploading":
      return <span className={cn(base, "text-fg-subtle tabular-nums")}>Uploading {Math.round(phase.progress * 100)}%</span>;
    case "checking":
      return <span className={cn(base, "text-fg-subtle")}><Spinner className="size-3" />Checking</span>;
    case "processing":
      return <span className={cn(base, "text-brand-strong")}><Spinner className="size-3" />Processing</span>;
    case "ready":
      return <span className={cn(base, "font-medium text-success")}><Check className="size-3.5" aria-hidden="true" />Ready</span>;
    case "duplicate":
      return <span className={cn(base, "text-fg-muted")}>Already indexed</span>;
    case "visibility_differs":
      return <span className={cn(base, "text-warning")} title="Change who can see it from the source page.">Already shared differently</span>;
    case "failed":
      return (
        <span className={cn(base, "font-medium text-danger")}>
          <CircleAlert className="size-3.5" aria-hidden="true" />
          Failed
        </span>
      );
  }
}
