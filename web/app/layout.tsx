import type { Metadata, Viewport } from "next";
import "./globals.css";
import { Nav } from "@/components/shell/nav";
import { themeScript } from "@/components/shell/theme";

export const metadata: Metadata = {
  title: { default: "Tracedown", template: "%s · Tracedown" },
  description: "The flight recorder for AI agents: find the step that caused a failure, then replay the run with a fix.",
};
export const viewport: Viewport = { width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head><script dangerouslySetInnerHTML={{ __html: themeScript }} /></head>
      <body>
        <Nav />
        <main className="md:pl-56">
          <div className="mx-auto max-w-[1400px] px-4 py-6 md:px-8">{children}</div>
        </main>
      </body>
    </html>
  );
}
