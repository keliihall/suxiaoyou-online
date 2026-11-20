"use client";

import { createContext, useContext } from "react";
import type { CloudIdentity } from "@/lib/cloud-session";

export const CloudIdentityContext = createContext<CloudIdentity | null>(null);
export const useCloudIdentity = () => useContext(CloudIdentityContext);
