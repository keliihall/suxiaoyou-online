import { redirect } from "next/navigation";
import { CloudRemoteAccess } from "./cloud-remote-access";

export default function RemotePage() {
  if (process.env.NEXT_PUBLIC_CLOUD_MODE === "true") return <CloudRemoteAccess />;
  redirect("/settings?tab=general");
}
