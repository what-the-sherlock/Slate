import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";
import { Slot } from "radix-ui";

// Variants and sizes follow docs/UI_DESIGN.md §3.1 (ADR-0017: dense 12px controls).
const buttonVariants = cva(
  [
    "relative inline-flex shrink-0 items-center justify-center gap-2 overflow-hidden whitespace-nowrap",
    "rounded-md border font-medium select-none outline-none",
    "transition-[background-color,border-color,color,transform] duration-150 ease-[var(--sl-ease-standard)]",
    "active:scale-[0.98] motion-reduce:active:scale-100",
    "disabled:pointer-events-none disabled:opacity-50 aria-busy:cursor-progress",
    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
    "[&_svg]:pointer-events-none [&_svg]:shrink-0",
  ].join(" "),
  {
    variants: {
      variant: {
        primary: "border-transparent bg-brand text-white shadow-xs hover:bg-brand-strong",
        secondary: "border-border bg-bg text-fg shadow-xs hover:border-border-strong hover:bg-subtle",
        ghost: "border-transparent bg-transparent text-fg-muted hover:bg-muted hover:text-fg",
        tinted: "border-transparent bg-brand-subtle text-brand hover:bg-brand-border/50",
        danger: "border-transparent bg-destructive text-white shadow-xs hover:brightness-95",
        link: "h-auto border-transparent p-0 text-brand underline-offset-[3px] hover:underline",
      },
      size: {
        sm: "h-7 px-2.5 text-caption [&_svg:not([class*='size-'])]:size-3.5",
        md: "h-8 px-3 text-caption [&_svg:not([class*='size-'])]:size-3.5",
        lg: "h-10 px-4 text-ui [&_svg:not([class*='size-'])]:size-4",
        icon: "size-8 border-border text-fg-muted hover:bg-muted [&_svg:not([class*='size-'])]:size-4",
        "icon-sm": "size-7 border-transparent text-fg-subtle hover:bg-muted hover:text-fg [&_svg:not([class*='size-'])]:size-3.5",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

type ButtonProps = React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & { asChild?: boolean; loading?: boolean };

function Button({
  className,
  variant,
  size,
  asChild = false,
  loading = false,
  disabled,
  children,
  ...props
}: ButtonProps) {
  const Comp = asChild ? Slot.Root : "button";
  return (
    <Comp
      data-slot="button"
      className={cn(buttonVariants({ variant, size }), className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {asChild ? (
        children
      ) : (
        <>
          {loading ? <Spinner /> : null}
          <span className="relative inline-flex items-center gap-2">{children}</span>
        </>
      )}
    </Comp>
  );
}

function Spinner({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 16 16"
      className={cn("relative size-3.5 animate-spin motion-reduce:animate-none", className)}
      aria-hidden="true"
    >
      <circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" strokeOpacity="0.25" strokeWidth="1.5" />
      <path d="M14.5 8A6.5 6.5 0 0 0 8 1.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

export { Button, buttonVariants, Spinner };
