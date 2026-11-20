"use client";

import { useEffect, useState, type ReactNode } from "react";
import { AppearanceInjector } from "@/components/layout/appearance-injector";
import { useAppearanceStore } from "@/stores/appearance-store";

/** Share the workbench's user-scoped appearance without booting its engine. */
export function CloudAppearanceProvider({ userId, children }: { userId?: string; children: ReactNode }) {
  const [hydratedUser, setHydratedUser] = useState<string>();
  useEffect(() => {
    if (!userId) return;
    let stopped = false;
    void Promise.resolve(useAppearanceStore.persist.rehydrate()).then(() => {
      if (!stopped) setHydratedUser(userId);
    }).catch(() => {
      // Keep the CSS defaults if this browser cannot read persisted appearance.
    });
    return () => { stopped = true; };
  }, [userId]);
  return <>{userId && hydratedUser === userId && <AppearanceInjector />}{children}</>;
}
