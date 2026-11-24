import type { Metadata } from "next";
import localFont from "next/font/local";
import { Geist_Mono } from "next/font/google";
import { Providers } from "./providers";
import "./globals.css";

// Inter Tight (SIL OFL), self-hosted from the Answerline export; see src/fonts/OFL.txt.
const interTight = localFont({
  src: [
    { path: "../fonts/inter-tight-400.woff2", weight: "400", style: "normal" },
    { path: "../fonts/inter-tight-500.woff2", weight: "500", style: "normal" },
    { path: "../fonts/inter-tight-600.woff2", weight: "600", style: "normal" },
  ],
  variable: "--font-inter-tight",
  display: "swap",
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
  weight: ["400", "500"],
});

export const metadata: Metadata = {
  title: { default: "Slate", template: "%s · Slate" },
  description: "Permission-aware knowledge search with cited answers.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${interTight.variable} ${geistMono.variable} h-full`}>
      <body className="flex min-h-full flex-col">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
