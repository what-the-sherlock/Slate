import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "cn";
import { Slot } from "radix-ui";

// Variants and sizes follow docs/UI_DESIGN.md §3.1.
const buttonVariants = cva(
  [
    "relative inline-flex shrink-0 items-center justify-center gap-2 overflow-hidden whitespace-nowrap",
    "rounded-md border font-medium select-none outline-none",
    "transition-[background-color,border-color,color,transform] duration-[var(--sl-dur-base)] ease-[var(--sl-ease-standard)]",
    "active:scale-[0.98] motion-reduce:active:scale-100",
    "disabled:pointer-events-none disabled:opacity-50 aria-busy:cursor-progress",
    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
    "[&_svg]:pointer-events-none [&_svg]:shrink-0",
  ].join(" "),
  {
    variants: {
      variant: {
        // Hover wash: a 14% white layer wipes in (Answerline technique).
        primary:
          "border-transparent bg-brand text-white before:absolute before:inset-0 before:bg-white/14 before:opacity-0 before:transition-opacity hover:before:opacity-100",
        secondary: "border-border-strong bg-bg text-fg hover:bg-emphasis",
        ghost: "border-transparent bg-transparent text-fg-muted hover:bg-muted hover:text-fg",
        tinted: "border-transparent bg-brand-subtle text-brand-strong hover:bg-brand-border/60",
        danger:
          "border-transparent bg-destructive text-white before:absolute before:inset-0 before:bg-white/14 before:opacity-0 before:transition-opacity hover:before:opacity-100",
        link: "h-auto border-transparent p-0 text-brand underline-offset-[3px] hover:underline",
      },
      size: {
        sm: "h-8 px-3 text-[13px] [&_svg:not([class*='size-'])]:size-3.5",
        md: "h-9 px-3.5 text-sm [&_svg:not([class*='size-'])]:size-4",
        lg: "h-11 px-5 text-[15px] [&_svg:not([class*='size-'])]:size-4",
        icon: "size-9 border-transparent text-fg-muted shadow-[inset_0_0_0_1px_var(--sl-border)] hover:bg-muted [&_svg:not([class*='size-'])]:size-4",
        "icon-sm": "size-8 border-transparent text-fg-muted hover:bg-muted [&_svg:not([class*='size-'])]:size-4",
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
