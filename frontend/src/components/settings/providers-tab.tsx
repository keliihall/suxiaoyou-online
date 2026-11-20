"use client";

import { useEffect, useState } from "react";
import {
  Cpu,
  Eye,
  Plug,
  Zap,
} from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { useSettingsStore } from "@/stores/settings-store";
import { api } from "@/lib/api";
import { API, queryKeys } from "@/lib/constants";
import { isByokProviderId, isCustomEndpointProviderId } from "@/lib/providers";
import { useModels } from "@/hooks/use-models";
import type { LocalProviderStatus, ProviderInfo } from "@/types/usage";
import type { ModelInfo } from "@/types/model";
import { OllamaPanel } from "@/components/settings/ollama-panel";
import { RapidMLXPanel } from "@/components/settings/rapid-mlx-panel";
import { ByokPanel } from "@/components/settings/providers/byok-panel";
import { CustomEndpointPanel } from "@/components/settings/providers/custom-endpoint-panel";
import { ChatGPTSubPanel } from "@/components/settings/providers/chatgpt-sub-panel";
import { IS_CLOUD } from "@/lib/cloud-session";
import Link from "next/link";

type ProviderMode = "byok" | "ollama" | "rapid-mlx" | "custom" | "chatgpt";

interface OllamaRuntimeStatus {
  binary_installed: boolean;
  running: boolean;
}
interface RapidMLXRuntimeStatus {
  running: boolean;
}

function pickModelForMode(
  mode: ProviderMode,
  models: ModelInfo[] | undefined,
): ModelInfo | null {
  if (!models || models.length === 0) return null;
  if (mode === "byok") {
    return models.find((m) => isByokProviderId(m.provider_id)) ?? null;
  }
  if (mode === "ollama") {
    return models.find((m) => m.provider_id === "ollama") ?? null;
  }
  if (mode === "rapid-mlx") {
    return models.find((m) => m.provider_id === "rapid-mlx") ?? null;
  }
  if (mode === "custom") {
    return models.find((m) => isCustomEndpointProviderId(m.provider_id)) ?? null;
  }
  if (mode === "chatgpt") return models.find(model => model.provider_id === "openai-subscription") ?? null;
  return null;
}

export function ProvidersTab() {
  const { t, i18n } = useTranslation("settings");
  const { activeProvider, setActiveProvider } = useSettingsStore();
  const normalizedActiveProvider: ProviderMode | null =
    activeProvider === "byok" ||
    activeProvider === "ollama" ||
    activeProvider === "rapid-mlx" ||
    (IS_CLOUD && activeProvider === "chatgpt") ||
    activeProvider === "custom"
      ? activeProvider
      : null;

  const [viewingProvider, setViewingProvider] = useState<ProviderMode>(
    () => normalizedActiveProvider ?? "byok",
  );
  const [mounted, setMounted] = useState(false);

  const { data: allModels } = useModels();

  const { data: providers } = useQuery({
    queryKey: queryKeys.providersForLanguage(
      i18n.resolvedLanguage || i18n.language,
    ),
    queryFn: () => api.get<ProviderInfo[]>(API.CONFIG.PROVIDERS),
  });
  const { data: localStatus } = useQuery({
    queryKey: queryKeys.localProvider,
    queryFn: () => api.get<LocalProviderStatus>(API.CONFIG.LOCAL_PROVIDER),
  });
  const { data: ollamaRuntimeStatus } = useQuery({
    queryKey: ["ollamaRuntime"],
    queryFn: () => api.get<OllamaRuntimeStatus>(API.OLLAMA.STATUS),
  });
  const { data: rapidMlxRuntimeStatus } = useQuery({
    queryKey: ["rapidMlxRuntime"],
    queryFn: () => api.get<RapidMLXRuntimeStatus>(API.RAPID_MLX.STATUS),
    retry: false,
  });
  const { data: subscription } = useQuery({
    queryKey: queryKeys.openaiSubscription,
    queryFn: () => api.get<{ is_connected: boolean }>(API.CONFIG.OPENAI_SUBSCRIPTION),
    enabled: IS_CLOUD,
    retry: false,
  });

  useEffect(() => {
    setMounted(true);
  }, []);

  const activateProviderMode = (mode: ProviderMode) => {
    setActiveProvider(mode);
    const picked = pickModelForMode(mode, allModels);
    if (picked) {
      useSettingsStore
        .getState()
        .setSelectedModel(picked.id, picked.provider_id);
    }
  };

  const fallbackToOtherProviders = () => {
    if (rapidMlxRuntimeStatus?.running) {
      setActiveProvider("rapid-mlx");
    } else if (
      localStatus?.is_connected ||
      (providers ?? []).some(
        (p) => p.id.startsWith("custom_") && p.is_configured,
      )
    ) {
      setActiveProvider("custom");
    } else if (
      (providers ?? []).some((p) => p.is_configured)
    ) {
      setActiveProvider("byok");
    } else {
      setActiveProvider(null);
    }
  };

  const ollamaConnected = !!ollamaRuntimeStatus?.running;
  const rapidMlxConnected = !!rapidMlxRuntimeStatus?.running;
  const customConnected =
    !!localStatus?.is_connected ||
    (providers ?? []).some(
      (p) => p.id.startsWith("custom_") && p.is_configured,
    );

  const modes: Array<{
    mode: ProviderMode;
    label: string;
    icon: typeof Eye;
    connected: boolean;
  }> = [
    ...(IS_CLOUD ? [{ mode: "chatgpt" as const, label: "ChatGPT", icon: Cpu, connected: !!subscription?.is_connected }] : []),
    {
      mode: "byok",
      label: t("ownApiKey"),
      icon: Eye,
      connected:
        (providers ?? []).some(
          (p) => p.is_configured && (!p.id.startsWith("custom_") || (IS_CLOUD && p.id === "custom_cloud")),
        ),
    },
    { mode: "ollama", label: "Ollama", icon: Cpu, connected: ollamaConnected },
    {
      mode: "rapid-mlx",
      label: "Rapid-MLX",
      icon: Zap,
      connected: rapidMlxConnected,
    },
    {
      mode: "custom",
      label: t("customEndpoint"),
      icon: Plug,
      connected: customConnected,
    },
  ];

  return (
    <div className="settings-section space-y-6">
      {/* Auxiliary copy retained in source: <p className="text-xs text-[var(--text-secondary)]">
        {t("providerModeDesc")}
      </p> */}

      {/* Provider cards */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {modes.map(({ mode, label, icon: Icon, connected }) => (
          <button
            key={mode}
            aria-pressed={viewingProvider === mode}
            onClick={() => {
              setViewingProvider(mode);
              if (connected) activateProviderMode(mode);
            }}
            className={`flex flex-col items-center min-h-28 gap-3 rounded-2xl border bg-[var(--surface-primary)] p-5 transition-colors relative ${
              viewingProvider === mode
                ? "border-[var(--border-heavy)] bg-[var(--surface-secondary)]"
                : "border-[var(--border-default)] hover:bg-[var(--surface-secondary)]"
            }`}
          >
            <Icon className="h-5 w-5" />
            <span className="text-xs font-medium text-center leading-tight">
              {label}
            </span>
            {mounted && connected && (
              <span className="absolute top-2 right-2 w-2 h-2 rounded-full bg-[var(--color-success)]" />
            )}
            {activeProvider === mode && mounted && connected && (
              <span className="absolute bottom-2 text-[12px] font-medium text-[var(--brand-primary)]">
                {t("activeProvider")}
              </span>
            )}
          </button>
        ))}
      </div>

      {(viewingProvider === "byok" || viewingProvider === "custom") && IS_CLOUD && <div className="surface-card flex flex-wrap gap-3 rounded-2xl border border-[var(--border-default)] p-5"><Link className="ui-control inline-flex items-center rounded-xl border border-[var(--border-default)] px-4 py-2 text-[13px] font-medium text-[var(--brand-primary)] hover:bg-[var(--brand-soft)]" href="/cloud?manage=1&tab=models">配置个人模型与 API Key</Link><Link className="ui-control inline-flex items-center rounded-xl border border-[var(--border-default)] px-4 py-2 text-[13px] font-medium text-[var(--brand-primary)] hover:bg-[var(--brand-soft)]" href="/cloud?manage=1">账户与共享模型</Link></div>}
      {viewingProvider === "byok" && !IS_CLOUD && (
        <ByokPanel
          providers={providers}
          onSaved={() => activateProviderMode("byok")}
        />
      )}
      {viewingProvider === "ollama" && <OllamaPanel />}
      {viewingProvider === "rapid-mlx" && <RapidMLXPanel />}
      {viewingProvider === "chatgpt" && IS_CLOUD && <ChatGPTSubPanel keyStatus={undefined} />}
      {viewingProvider === "custom" && !IS_CLOUD && (
        <CustomEndpointPanel
          providers={providers}
          localStatus={localStatus}
          onLocalDeleted={() => {
            if (activeProvider === "custom") fallbackToOtherProviders();
          }}
        />
      )}
    </div>
  );
}
