import type { Metadata } from "next";
import localFont from "next/font/local";
import { Toaster } from "@/components/ui/sonner";
import { Providers } from "./providers";
import "./globals.css";

// Inter and JetBrains Mono (both SIL OFL), self-hosted from src/fonts, the same files
// Seekworthy ships (ADR-0017). Variable weight, Latin subset; see src/fonts/OFL.txt.
const inter = localFont({
  src: [
    { path: "../fonts/Inter-roman-100-900-latin.woff2", weight: "100 900", style: "normal" },
    { path: "../fonts/Inter-italic-100-900-latin.woff2", weight: "100 900", style: "italic" },
  ],
  variable: "--font-inter",
  display: "swap",
});

const mono = localFont({
  src: [{ path: "../fonts/JetBrainsMono-roman-100-800-latin.woff2", weight: "100 800", style: "normal" }],
  variable: "--font-jetbrains-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: { default: "Slate", template: "%s · Slate" },
  description: "Permission-aware knowledge search with cited answers.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${inter.variable} ${mono.variable} h-full`}>
      <body className="flex min-h-full flex-col">
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-[90] focus:rounded-md focus:bg-bg focus:px-3 focus:py-2 focus:shadow-md"
        >
          Skip to content
        </a>
        <Providers>{children}</Providers>
        <Toaster />
      </body>
    </html>
  );
}
