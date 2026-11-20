import DesktopMobileLayout from "./desktop-mobile-layout";
import { CloudSessionBoundary } from "@/components/providers/cloud-session-boundary";

export default function Layout({ children }: { children: React.ReactNode }) {
  if (process.env.NEXT_PUBLIC_CLOUD_MODE === "true") return <CloudSessionBoundary>{children}</CloudSessionBoundary>;
  return <DesktopMobileLayout>{children}</DesktopMobileLayout>;
}
