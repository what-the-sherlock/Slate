"use client";

import { CircleAlert, CircleCheck, Info, Loader2, TriangleAlert } from "lucide-react";
import { Toaster as Sonner, type ToasterProps } from "sonner";

// Toasts per docs/UI_DESIGN.md §3.5: bottom-right, 360px, max 3 visible, light theme only.
function Toaster(props: ToasterProps) {
  return (
    <Sonner
      theme="light"
      position="bottom-right"
      visibleToasts={3}
      duration={4000}
      offset={24}
      icons={{
        success: <CircleCheck className="size-4 text-success" />,
        info: <Info className="size-4 text-brand" />,
        warning: <TriangleAlert className="size-4 text-warning" />,
        error: <CircleAlert className="size-4 text-danger" />,
        loading: <Loader2 className="size-4 animate-spin text-brand" />,
      }}
      toastOptions={{
        classNames: {
          toast:
            "!w-[360px] !rounded-lg !border !border-border !bg-bg !px-3.5 !py-3 !shadow-md !font-sans",
          title: "!text-sm !font-medium !text-fg",
          description: "!text-[13px] !text-fg-subtle",
          actionButton: "!h-8 !rounded-md !bg-bg !text-fg !border !border-border-strong",
        },
      }}
      {...props}
    />
  );
}

export { Toaster };
