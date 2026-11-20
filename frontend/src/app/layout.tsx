import type { Metadata } from "next";
import "./globals.css";

const cloudMode = process.env.NEXT_PUBLIC_CLOUD_MODE === "true";

export const metadata: Metadata = {
  title: cloudMode ? "苏小有 · 云工作台" : "suyo",
  description: cloudMode ? "文件、对话与任务，汇聚在你的私人云工作区" : "Local-first AI agent for desktop files, tools, and long-running work",
  icons: {
    icon: "/favicon.svg",
  },
  manifest: "/manifest.json",
};

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // Cloud has its own cookie-auth state and never mounts the desktop lifecycle,
  // provider detection, local backend hydration, or stream registries.
  const AppProviders = cloudMode ? null : (await import("@/components/providers/app-providers")).AppProviders;
  return (
    <html lang={cloudMode ? "zh-CN" : "en"} suppressHydrationWarning>
      <body className="font-sans antialiased">
        {AppProviders ? <AppProviders>{children}</AppProviders> : children}
      </body>
    </html>
  );
}
