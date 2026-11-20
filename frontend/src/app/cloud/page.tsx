import CloudApp from "./cloud-app";
import "./cloud.css";

export default function CloudPage() {
  if (process.env.NEXT_PUBLIC_CLOUD_MODE !== "true") {
    return <main style={{ padding: 40 }}>当前未启用云端版本。</main>;
  }
  return <CloudApp />;
}
