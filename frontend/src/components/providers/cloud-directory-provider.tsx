"use client";

import { useEffect, useRef, useState } from "react";
import { MobileDirectoryBrowser } from "@/components/mobile/directory-browser";
import { IS_CLOUD } from "@/lib/cloud-session";
import { CLOUD_DIRECTORY_EVENT, type CloudDirectoryRequest } from "@/lib/upload";

/** One browser workspace picker shared by onboarding, toolbar and composer. */
export function CloudDirectoryProvider() {
  const [request, setRequest] = useState<CloudDirectoryRequest | null>(null);
  const active = useRef<CloudDirectoryRequest | null>(null);

  useEffect(() => {
    if (!IS_CLOUD) return;
    const receive = (event: Event) => {
      const next = (event as CustomEvent<CloudDirectoryRequest>).detail;
      active.current?.resolve(null);
      active.current = next;
      setRequest(next);
    };
    window.addEventListener(CLOUD_DIRECTORY_EVENT, receive);
    return () => {
      window.removeEventListener(CLOUD_DIRECTORY_EVENT, receive);
      active.current?.resolve(null);
      active.current = null;
    };
  }, []);

  const finish = (path: string | null) => {
    active.current?.resolve(path);
    active.current = null;
    setRequest(null);
  };

  return request ? <MobileDirectoryBrowser open title={request.title}
    initialPath={request.initialPath} onClose={() => finish(null)} onSelect={path => finish(path)} /> : null;
}
