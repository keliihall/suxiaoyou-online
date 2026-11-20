"use client";

import { useState } from "react";
import { AlertCircle, Check, Eye, EyeOff, Loader2, X } from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { api } from "@/lib/api";
import { errorToMessage } from "@/lib/errors";
import { API, queryKeys } from "@/lib/constants";
import type { ProviderInfo } from "@/types/usage";

interface ByokPanelProps {
  providers: ProviderInfo[] | undefined;
  onSaved: () => void;
}

export function ByokPanel({ providers, onSaved }: ByokPanelProps) {
  const { t } = useTranslation("settings");
  const qc = useQueryClient();

  const [keyInputs, setKeyInputs] = useState<Record<string, string>>({});
  const [showKey, setShowKey] = useState<Record<string, boolean>>({});
  const [mutatingId, setMutatingId] = useState<string | null>(null);
  const [providerError, setProviderError] = useState<Record<string, string>>(
    {},
  );

  const updateProviderKey = useMutation({
    mutationFn: async ({ id, apiKey }: { id: string; apiKey: string }) => {
      setMutatingId(id);
      return api.post<ProviderInfo>(API.CONFIG.PROVIDER_KEY(id), {
        api_key: apiKey,
      });
    },
    onSuccess: (_data, { id }) => {
      setKeyInputs((prev) => ({ ...prev, [id]: "" }));
      setProviderError((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
      setMutatingId(null);
      onSaved();
      qc.invalidateQueries({ queryKey: queryKeys.providers });
      qc.invalidateQueries({ queryKey: queryKeys.models });
    },
    onError: (err, { id }) => {
      setMutatingId(null);
      const detail = errorToMessage(err, t("failedSaveKey"));
      setProviderError((prev) => ({ ...prev, [id]: detail }));
    },
  });

  const deleteProviderKey = useMutation({
    mutationFn: async (id: string) => {
      setMutatingId(id);
      return api.delete<ProviderInfo>(API.CONFIG.PROVIDER_KEY(id));
    },
    onSuccess: () => {
      setMutatingId(null);
      qc.invalidateQueries({ queryKey: queryKeys.providers });
      qc.invalidateQueries({ queryKey: queryKeys.models });
    },
    onError: () => setMutatingId(null),
  });

  const toggleProvider = useMutation({
    mutationFn: (id: string) =>
      api.post<ProviderInfo>(API.CONFIG.PROVIDER_TOGGLE(id)),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.providers });
      qc.invalidateQueries({ queryKey: queryKeys.models });
    },
  });

  const byokProviders = (providers ?? []).filter(
    (p) => !p.id.startsWith("custom_"),
  );

  return (
    <div className="space-y-4">
      {/* Auxiliary copy retained in source: <p className="text-xs text-[var(--text-secondary)]">{t("byokDesc")}</p> */}

      {byokProviders.map((p) => (
        <div
          key={p.id}
          className="space-y-3 rounded-2xl border border-[var(--border-default)] bg-[var(--surface-primary)] p-4"
        >
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-[var(--text-primary)]">
              {p.name}
            </span>
            <div className="flex items-center gap-2">
              {p.is_configured && p.enabled && (
                <span className="text-[12px] text-[var(--text-tertiary)]">
                  {p.model_count} {t("providerModels")}
                </span>
              )}
              {p.is_configured && (
                <Switch
                  checked={p.enabled}
                  onCheckedChange={() => toggleProvider.mutate(p.id)}
                  disabled={toggleProvider.isPending}
                  aria-label={
                    p.enabled ? t("disableProvider") : t("enableProvider")
                  }
                />
              )}
            </div>
          </div>
          {p.is_configured && (
            <div className="flex items-center gap-2 text-xs">
              <Check className="h-3.5 w-3.5 text-[var(--color-success)]" />
              <span className="text-[var(--text-secondary)] font-mono">
                {p.masked_key}
              </span>
              <button
                onClick={() => deleteProviderKey.mutate(p.id)}
                disabled={mutatingId === p.id}
                className="ml-1 text-[var(--text-tertiary)] hover:text-[var(--color-destructive)] transition-colors"
                title={t("removeApiKey")}
              >
                {mutatingId === p.id ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <X className="h-3.5 w-3.5" />
                )}
              </button>
            </div>
          )}
          <div className="flex items-start gap-2">
            <div className="flex-1 space-y-2">
              <div className="relative">
                <Input
                  type={showKey[p.id] ? "text" : "password"}
                  value={keyInputs[p.id] ?? ""}
                  onChange={(e) =>
                    setKeyInputs((prev) => ({
                      ...prev,
                      [p.id]: e.target.value,
                    }))
                  }
                  placeholder={t(`providerKeyPlaceholder_${p.id}`, {
                    defaultValue: `${p.name} API key`,
                  })}
                  className="pr-12 font-mono text-xs"
                  autoComplete="one-time-code"
                  data-form-type="other"
                />
                <button
                  type="button"
                  onClick={() =>
                    setShowKey((prev) => ({
                      ...prev,
                      [p.id]: !prev[p.id],
                    }))
                  }
                  aria-label={t("customApiKeyLabel", { defaultValue: "API key" })}
                  aria-pressed={showKey[p.id]}
                  className="absolute right-1 top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-lg text-[var(--text-secondary)] hover:bg-[var(--surface-secondary)] hover:text-[var(--text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]"
                >
                  {showKey[p.id] ? (
                    <EyeOff className="h-3.5 w-3.5" />
                  ) : (
                    <Eye className="h-3.5 w-3.5" />
                  )}
                </button>
              </div>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                updateProviderKey.mutate({
                  id: p.id,
                  apiKey: keyInputs[p.id] ?? "",
                })
              }
              disabled={
                !(keyInputs[p.id] ?? "").trim() || mutatingId === p.id
              }
            >
              {mutatingId === p.id ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                t("save")
              )}
            </Button>
          </div>
          {providerError[p.id] && (
            <div className="flex items-center gap-1.5 text-xs text-[var(--color-destructive)]">
              <AlertCircle className="h-3.5 w-3.5 shrink-0" />
              <span>{providerError[p.id]}</span>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
