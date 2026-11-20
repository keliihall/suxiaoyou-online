"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";
import {
  ArrowLeft, Camera, CheckCircle2, XCircle, Loader2,
  Wifi, WifiOff, KeyRound, Link2, Trash2, Cpu, ChevronRight, UserRound, SlidersHorizontal, Cloud, LogOut,
} from "lucide-react";
import { toast } from "sonner";
import {
  CHINA_REMOTE_PROVIDERS,
  getRemoteConfig,
  saveRemoteConfig,
  clearRemoteConfig,
  parseQRData,
  autoConnectFromUrl,
  isRemoteMode,
  getRemoteProvider,
  saveRemoteProvider,
  isRemoteProvider,
  type RemoteConfig,
  type RemoteProvider,
} from "@/lib/remote-connection";
import { api } from "@/lib/api";
import { API } from "@/lib/constants";
import { useSettingsStore } from "@/stores/settings-store";
import type { ModelInfo } from "@/types/model";
import { IS_CLOUD, logoutCloudWorkspace } from "@/lib/cloud-session";
import { useCloudIdentity } from "@/components/providers/cloud-identity-context";
import { useProviderModels } from "@/hooks/use-provider-models";

export default function MobileSettingsPage() {
  return IS_CLOUD ? <CloudMobileSettings /> : <RemoteMobileSettings />;
}

function CloudMobileSettings() {
  const user = useCloudIdentity();
  const { data: models } = useProviderModels();
  const selectedModel = useSettingsStore(state => state.selectedModel);
  const selectModel = useSettingsStore(state => state.setSelectedModel);
  return (
    <main className="flex h-full min-h-0 flex-col bg-[var(--surface-secondary)]">
      <header className="flex shrink-0 items-center gap-3 border-b border-[var(--border-default)] bg-[var(--surface-primary)] px-4 pb-3 pt-[max(env(safe-area-inset-top),16px)]">
        <a href="/m" aria-label="任务列表" className="icon-action flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)]"><ArrowLeft className="h-5 w-5" aria-hidden="true" /></a>
        <h1 className="text-lg font-semibold tracking-tight">账户设置</h1>
      </header>
      <div className="mx-auto flex min-h-0 w-full max-w-3xl flex-1 flex-col gap-4 overflow-y-auto px-4 pt-6 pb-[max(env(safe-area-inset-bottom),16px)]">
        <div className="surface-card flex items-center gap-4 p-4">
          <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-[var(--brand-soft)] text-[var(--brand-primary)]"><UserRound className="h-6 w-6" aria-hidden="true" /></div>
          <p className="min-w-0 break-words text-[15px] font-semibold">{user?.display_name || user?.email}</p>
        </div>
        <label className="surface-card flex flex-col gap-3 p-4 text-sm font-medium">模型
          <select className="min-h-12 w-full rounded-[var(--radius-control)] border border-[var(--border-default)] bg-[var(--surface-secondary)] px-3 py-3 text-[16px]" value={selectedModel || ""} onChange={event => { const model = models.find(item => item.id === event.target.value); selectModel(event.target.value, model?.provider_id); }}>
            <option value="" disabled>选择模型</option>
            {models.map(model => <option key={`${model.provider_id}:${model.id}`} value={model.id}>{model.name}</option>)}
          </select>
        </label>
        <nav aria-label="账户设置" className="surface-card divide-y divide-[var(--border-default)] overflow-hidden">
          <a href="/settings" className="flex min-h-14 items-center gap-3 px-4 py-4 text-[15px] font-medium transition-colors hover:bg-[var(--brand-soft)]"><SlidersHorizontal className="h-[18px] w-[18px] text-[var(--text-secondary)]" aria-hidden="true" /><span className="flex-1">完整设置</span><ChevronRight className="h-4 w-4 text-[var(--text-tertiary)]" aria-hidden="true" /></a>
          <a href="/cloud?manage=1" className="flex min-h-14 items-center gap-3 px-4 py-4 text-[15px] font-medium transition-colors hover:bg-[var(--brand-soft)]"><Cloud className="h-[18px] w-[18px] text-[var(--text-secondary)]" aria-hidden="true" /><span className="flex-1">{user?.role === "admin" ? "云端管理" : "用量与额度"}</span><ChevronRight className="h-4 w-4 text-[var(--text-tertiary)]" aria-hidden="true" /></a>
        </nav>
        <button onClick={() => void logoutCloudWorkspace()} className="surface-card flex min-h-14 items-center gap-3 px-4 py-4 text-left text-[15px] font-medium text-[var(--color-destructive)] transition-colors hover:bg-[var(--surface-tertiary)]"><LogOut className="h-[18px] w-[18px]" aria-hidden="true" />退出登录</button>
      </div>
    </main>
  );
}

function RemoteMobileSettings() {
  const router = useRouter();
  const { t } = useTranslation("settings");
  const [config, setConfig] = useState<RemoteConfig | null>(null);
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<"success" | "error" | null>(null);
  const [scanning, setScanning] = useState(false);
  const scanningRef = useRef(false);
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);

  // Provider state
  const [activeProvider, setActiveProvider] = useState<RemoteProvider | null>(null);
  const [availableProviders, setAvailableProviders] = useState<{ id: RemoteProvider; icon: typeof Cpu; count: number }[]>([]);
  const [allModels, setAllModels] = useState<ModelInfo[]>([]);

  useEffect(() => {
    // Auto-connect from URL token (e.g., pasted link)
    autoConnectFromUrl();

    const existing = getRemoteConfig();
    if (existing) {
      setConfig(existing);
      setUrl(existing.url);
      setToken(existing.token);
    }
    // Load saved provider preference
    const saved = getRemoteProvider();
    if (saved) setActiveProvider(saved);
  }, []);

  // Fetch available providers when connected
  useEffect(() => {
    if (!isRemoteMode()) return;
    api.get<ModelInfo[]>(API.MODELS)
      .then((models) => {
        if (!Array.isArray(models)) return;
        setAllModels(models);
        const providers: typeof availableProviders = [];
        for (const id of CHINA_REMOTE_PROVIDERS) {
          const providerModels = models.filter((m) => m.provider_id === id);
          if (providerModels.length > 0) {
            providers.push({
              id,
              icon: Cpu,
              count: providerModels.length,
            });
          }
        }
        setAvailableProviders(providers);
        // Auto-select if no preference saved
        if ((!activeProvider || !isRemoteProvider(activeProvider)) && providers.length > 0) {
          const defaultP = providers[0];
          setActiveProvider(defaultP.id);
          saveRemoteProvider(defaultP.id);
        }
      })
      .catch(() => {});
  }, [activeProvider, config]);

  const handleProviderSelect = (id: RemoteProvider) => {
    setActiveProvider(id);
    saveRemoteProvider(id);
    // Sync to Zustand so ChatView's ModelSelector filters correctly
    const store = useSettingsStore.getState();
    store.setActiveProvider("byok");
    // Also set selectedModel to first model of this provider
    const firstModel = allModels.find((m) => m.provider_id === id);
    if (firstModel) {
      store.setSelectedModel(firstModel.id, firstModel.provider_id);
    }
  };

  const handleConnect = useCallback(async (connectUrl: string, connectToken: string) => {
    if (!connectUrl || !connectToken) return;
    setTesting(true);
    setTestResult(null);

    try {
      const res = await fetch(`${connectUrl}/health`, {
        headers: { Authorization: `Bearer ${connectToken}` },
        signal: AbortSignal.timeout(10000),
      });
      if (res.ok) {
        const newConfig = { url: connectUrl, token: connectToken };
        saveRemoteConfig(newConfig);
        setConfig(newConfig);
        setTestResult("success");
        setTimeout(() => router.push("/m"), 800);
      } else {
        setTestResult("error");
        toast.error(res.status === 401 ? t("mobileInvalidToken") : t("mobileConnectionFailed"));
      }
    } catch {
      setTestResult("error");
      toast.error(t("mobileServiceUnreachable"));
    } finally {
      setTesting(false);
    }
  }, [router, t]);

  const handleDisconnect = useCallback(() => {
    clearRemoteConfig();
    setConfig(null);
    setUrl("");
    setToken("");
    setTestResult(null);
  }, []);

  const stopScan = useCallback(() => {
    setScanning(false);
    scanningRef.current = false;
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    }
  }, []);

  const startScan = useCallback(async () => {
    setScanning(true);
    scanningRef.current = true;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "environment" },
      });
      streamRef.current = stream;
      if (videoRef.current) {
        videoRef.current.srcObject = stream;
        await videoRef.current.play();
      }
      if ("BarcodeDetector" in window) {
        // BarcodeDetector is a browser API not yet in TypeScript's lib definitions
        const detector = new (window as unknown as { BarcodeDetector: new (opts: { formats: string[] }) => { detect: (source: HTMLVideoElement) => Promise<Array<{ rawValue: string }>> } }).BarcodeDetector({ formats: ["qr_code"] });
        const scanLoop = async () => {
          if (!videoRef.current || !scanningRef.current) return;
          try {
            const barcodes = await detector.detect(videoRef.current);
            if (barcodes.length > 0) {
              const parsed = parseQRData(barcodes[0].rawValue);
              if (parsed) {
                stopScan();
                setUrl(parsed.url);
                setToken(parsed.token);
                handleConnect(parsed.url, parsed.token);
                return;
              }
            }
          } catch {}
          if (scanningRef.current) requestAnimationFrame(scanLoop);
        };
        requestAnimationFrame(scanLoop);
      } else {
        toast.error(t("mobileQrUnsupported"));
        stopScan();
      }
    } catch {
      toast.error(t("mobileCameraUnavailable"));
      setScanning(false);
      scanningRef.current = false;
    }
  }, [handleConnect, stopScan, t]);

  useEffect(() => {
    return () => {
      if (streamRef.current) {
        streamRef.current.getTracks().forEach((t) => t.stop());
      }
    };
  }, []);

  const isConnected = config !== null;

  return (
    <main className="flex h-full min-h-0 flex-col bg-[var(--surface-secondary)]">
      {/* Header */}
      <header className="flex shrink-0 items-center gap-3 border-b border-[var(--border-default)] bg-[var(--surface-primary)] px-4 pb-3 pt-[max(env(safe-area-inset-top),16px)]">
        {isRemoteMode() && (
          <button
            onClick={() => router.push("/m")}
            className="icon-action flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] hover:bg-[var(--surface-secondary)]"
            aria-label={t("back")}
          >
            <ArrowLeft className="w-5 h-5" />
          </button>
        )}
        <h1 className="text-lg font-semibold tracking-tight">{t("mobileRemoteTitle")}</h1>
      </header>

      <div className="mx-auto min-h-0 w-full max-w-3xl flex-1 space-y-6 overflow-y-auto px-4 pt-6 pb-[max(env(safe-area-inset-bottom),16px)]">
        {/* Connection status card */}
        <div className="surface-card p-4" role="status">
          <div className="flex items-center gap-3">
            {isConnected ? (
              <div className="h-10 w-10 rounded-full bg-emerald-500/10 flex items-center justify-center shrink-0">
                <Wifi className="w-5 h-5 text-emerald-500" />
              </div>
            ) : (
              <div className="h-10 w-10 rounded-full bg-[var(--surface-tertiary)] flex items-center justify-center shrink-0">
                <WifiOff className="w-5 h-5 text-[var(--text-tertiary)]" />
              </div>
            )}
            <div className="flex-1 min-w-0">
              <p className="text-[15px] font-medium">
                {isConnected ? t("mobileConnected") : t("mobileDisconnected")}
              </p>
              {isConnected && (
                <p className="text-[12px] text-[var(--text-tertiary)] truncate mt-0.5">
                  {config.url}
                </p>
              )}
            </div>
            {isConnected && (
              <button
                onClick={handleDisconnect}
                className="icon-action flex h-11 w-11 shrink-0 items-center justify-center rounded-[var(--radius-control)] text-[var(--text-secondary)] hover:bg-red-500/10 hover:text-[var(--color-destructive)]"
                aria-label={t("disconnect")}
              >
                <Trash2 className="w-4 h-4" />
              </button>
            )}
          </div>
        </div>

        {/* QR Scanner */}
        {!isConnected && (
          <div className="space-y-3">
            <p className="text-[13px] font-medium text-[var(--text-secondary)] px-1">
              {t("mobileScanQr")}
            </p>

            {scanning ? (
              <div className="relative rounded-2xl overflow-hidden bg-black aspect-square max-w-[280px] mx-auto shadow-[var(--shadow-lg)]">
                <video ref={videoRef} className="w-full h-full object-cover" playsInline muted />
                <div className="absolute inset-4 border-2 border-white/20 rounded-xl" />
                <button
                  onClick={stopScan}
                  className="absolute bottom-4 left-1/2 min-h-11 -translate-x-1/2 rounded-[var(--radius-control)] bg-black/60 px-5 py-3 text-sm font-medium text-white backdrop-blur-sm"
                >
                  {t("channelCancel")}
                </button>
              </div>
            ) : (
              <button
                onClick={startScan}
                className="surface-card flex min-h-12 w-full items-center justify-center gap-3 py-4 text-sm font-medium transition-colors hover:border-[var(--brand-border)] hover:bg-[var(--brand-soft)]"
              >
                <Camera className="w-[18px] h-[18px] text-[var(--text-secondary)]" />
                {t("mobileOpenCamera")}
              </button>
            )}
          </div>
        )}

        {/* Manual input */}
        {!isConnected && (
          <div className="space-y-3">
            <p className="text-[13px] font-medium text-[var(--text-secondary)] px-1">
              {t("mobileManualEntry")}
            </p>

            <div className="space-y-2.5">
              <div className="flex items-center gap-3 rounded-[var(--radius-control)] border border-[var(--border-default)] bg-[var(--surface-primary)] px-4 py-3 focus-within:border-[var(--brand-primary)] transition-colors">
                <Link2 className="w-4 h-4 text-[var(--text-tertiary)] shrink-0" />
                <input
                  type="url"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="https://xxx.trycloudflare.com"
                  aria-label={t("mobileServerUrl")}
                  autoCapitalize="none"
                  autoCorrect="off"
                  className="min-w-0 flex-1 bg-transparent text-[16px] outline-none placeholder:text-[var(--text-tertiary)]"
                />
              </div>

              <div className="flex items-center gap-3 rounded-[var(--radius-control)] border border-[var(--border-default)] bg-[var(--surface-primary)] px-4 py-3 focus-within:border-[var(--brand-primary)] transition-colors">
                <KeyRound className="w-4 h-4 text-[var(--text-tertiary)] shrink-0" />
                <input
                  type="password"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  placeholder="suxiaoyou_rt_..."
                  aria-label={t("mobileAccessToken")}
                  autoComplete="off"
                  className="min-w-0 flex-1 bg-transparent text-[16px] outline-none placeholder:text-[var(--text-tertiary)]"
                />
              </div>

              <button
                onClick={() => handleConnect(url.trim(), token.trim())}
                disabled={!url.trim() || !token.trim() || testing}
                aria-busy={testing}
                className="flex min-h-12 w-full items-center justify-center gap-2 rounded-[var(--radius-control)] bg-[var(--brand-primary)] py-3 text-sm font-medium text-[var(--brand-primary-text)] transition-colors hover:bg-[var(--brand-primary-hover)] disabled:bg-[var(--surface-tertiary)] disabled:text-[var(--text-secondary)] disabled:cursor-not-allowed"
              >
                {testing ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : testResult === "success" ? (
                  <CheckCircle2 className="w-4 h-4" />
                ) : testResult === "error" ? (
                  <XCircle className="w-4 h-4" />
                ) : null}
                {testing
                  ? t("mobileConnecting")
                  : testResult === "success"
                    ? t("mobileConnected")
                    : testResult === "error"
                      ? t("mobileConnectionFailed")
                      : t("mobileConnect")}
              </button>
            </div>
          </div>
        )}

        {/* Provider selector — only show when connected */}
        {isConnected && availableProviders.length > 0 && (
          <div className="space-y-3">
            <p className="text-[13px] font-medium text-[var(--text-secondary)] px-1">
              {t("mobileModelProvider")}
            </p>
            <div className="space-y-2">
              {availableProviders.map((p) => {
                const isActive = activeProvider === p.id;
                const Icon = p.icon;
                return (
                  <button
                    key={p.id}
                    onClick={() => handleProviderSelect(p.id)}
                    aria-pressed={isActive}
                    className={`w-full flex items-center gap-3 p-4 rounded-[var(--radius-card)] border transition-colors ${
                      isActive
                        ? "border-[var(--brand-border)] bg-[var(--brand-soft)]"
                        : "border-[var(--border-default)] bg-[var(--surface-primary)] hover:bg-[var(--brand-soft)]"
                    }`}
                  >
                    <div className={`h-10 w-10 rounded-xl flex items-center justify-center shrink-0 ${
                      isActive ? "bg-[var(--brand-primary)] text-[var(--brand-primary-text)]" : "bg-[var(--surface-tertiary)] text-[var(--text-secondary)]"
                    }`}>
                      <Icon className="w-5 h-5" />
                    </div>
                    <div className="flex-1 text-left min-w-0">
                      <p className="text-[15px] font-medium">{t(`providerName_${p.id}`)}</p>
                      <p className="text-[12px] text-[var(--text-tertiary)]">
                        {t("mobileModelCount", { count: p.count })}
                      </p>
                    </div>
                    {isActive && (
                      <div className="h-2 w-2 rounded-full bg-emerald-500 shrink-0" />
                    )}
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {/* Auxiliary connection instructions remain in source only:
            t("mobileHowToConnect"), t("mobileConnectStep1"),
            t("mobileConnectStep2"), t("mobileConnectStep3"). */}
      </div>
    </main>
  );
}
