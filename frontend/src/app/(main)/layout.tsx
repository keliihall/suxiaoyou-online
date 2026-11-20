import DesktopMainLayout from "./desktop-main-layout";
import { CloudSessionBoundary } from "@/components/providers/cloud-session-boundary";

export default function Layout({ children }: { children: React.ReactNode }) {
  if (process.env.NEXT_PUBLIC_CLOUD_MODE === "true") return <CloudSessionBoundary><DesktopMainLayout>{children}</DesktopMainLayout></CloudSessionBoundary>;
  return <DesktopMainLayout>{children}</DesktopMainLayout>;
}
