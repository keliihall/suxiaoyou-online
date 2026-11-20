"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import Image from "next/image";
import { Check, Loader2, RefreshCw } from "lucide-react";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { CloudError, cloudRequest } from "./cloud-api";

interface CaptchaChallenge {
  challenge_id: string;
  background_image: string;
  piece_image: string;
  image_width: number;
  image_height: number;
  piece_width: number;
  piece_height: number;
  piece_y: number;
  expires_in: number;
}

interface CaptchaVerification {
  proof: string;
  expires_in: number;
}

type Phase = "loading" | "ready" | "verifying" | "verified" | "error" | "expired";
const REQUEST_TIMEOUT_MS = 10_000;

function isValidChallenge(challenge: CaptchaChallenge) {
  return typeof challenge.challenge_id === "string" && !!challenge.challenge_id
    && challenge.background_image?.startsWith("data:image/png;base64,")
    && challenge.piece_image?.startsWith("data:image/png;base64,")
    && [challenge.image_width, challenge.image_height, challenge.piece_width, challenge.piece_height, challenge.piece_y, challenge.expires_in].every(Number.isFinite)
    && challenge.image_width > challenge.piece_width && challenge.piece_width > 0
    && challenge.image_height >= challenge.piece_height && challenge.piece_height > 0
    && challenge.piece_y >= 0 && challenge.piece_y + challenge.piece_height <= challenge.image_height
    && challenge.expires_in > 0;
}

function failureMessage(error: unknown) {
  if (error instanceof CloudError) {
    if (error.code === "captcha_rate_limited" || error.status === 429) return "操作频繁，请稍后再试";
    if (error.code === "captcha_failed") return "验证未通过，请刷新重试";
    if (error.code === "captcha_required") return "验证已过期，请刷新";
  }
  return "验证码加载失败，请刷新";
}

export function CloudLoginCaptcha({ disabled, resetKey, onProofChange }: { disabled: boolean; resetKey: number; onProofChange: (proof: string | null) => void }) {
  const sliderId = useId();
  const hintId = useId();
  const [challenge, setChallenge] = useState<CaptchaChallenge | null>(null);
  const [phase, setPhase] = useState<Phase>("loading");
  const [message, setMessage] = useState("");
  const [position, setPosition] = useState(0);
  const [expiresAt, setExpiresAt] = useState<number | null>(null);
  const [loadedImages, setLoadedImages] = useState({ background: false, piece: false });
  const requestVersion = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);
  const verifyingRef = useRef(false);
  const imagesReady = loadedImages.background && loadedImages.piece;
  const maxPosition = challenge ? challenge.image_width - challenge.piece_width : 1;

  const refresh = useCallback(async () => {
    const version = ++requestVersion.current;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    verifyingRef.current = false;
    onProofChange(null);
    setPhase("loading");
    setMessage("");
    setChallenge(null);
    setPosition(0);
    setExpiresAt(null);
    setLoadedImages({ background: false, piece: false });
    const startedAt = Date.now();
    let timedOut = false;
    const timer = window.setTimeout(() => { timedOut = true; controller.abort(); }, REQUEST_TIMEOUT_MS);
    try {
      const next = await cloudRequest<CaptchaChallenge>("/auth/captcha/challenge", { signal: controller.signal });
      if (version !== requestVersion.current) return;
      if (!isValidChallenge(next)) throw new Error("Invalid captcha image");
      setChallenge(next);
      setExpiresAt(startedAt + next.expires_in * 1000);
      setPhase("ready");
    } catch (error) {
      if (version !== requestVersion.current || (controller.signal.aborted && !timedOut)) return;
      setMessage(timedOut ? "连接超时，请刷新" : failureMessage(error));
      setPhase("error");
    } finally {
      window.clearTimeout(timer);
    }
  }, [onProofChange]);

  useEffect(() => {
    void refresh();
    return () => {
      requestVersion.current += 1;
      controllerRef.current?.abort();
    };
  }, [refresh, resetKey]);

  useEffect(() => {
    if (expiresAt === null) return;
    const expire = () => {
      requestVersion.current += 1;
      controllerRef.current?.abort();
      verifyingRef.current = false;
      onProofChange(null);
      setPhase("expired");
      setExpiresAt(null);
    };
    const timer = window.setTimeout(expire, Math.max(0, expiresAt - Date.now()));
    return () => window.clearTimeout(timer);
  }, [expiresAt, onProofChange]);

  const verify = async (x: number) => {
    if (disabled || phase !== "ready" || !challenge || !imagesReady || verifyingRef.current) return;
    if (expiresAt === null || Date.now() >= expiresAt) {
      onProofChange(null);
      setPhase("expired");
      return;
    }
    verifyingRef.current = true;
    const version = ++requestVersion.current;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const nextPosition = Math.max(0, Math.min(maxPosition, Math.round(x)));
    const startedAt = Date.now();
    let timedOut = false;
    const timer = window.setTimeout(() => { timedOut = true; controller.abort(); }, REQUEST_TIMEOUT_MS);
    setPosition(nextPosition);
    setPhase("verifying");
    onProofChange(null);
    try {
      const result = await cloudRequest<CaptchaVerification>("/auth/captcha/verify", {
        method: "POST",
        body: JSON.stringify({ challenge_id: challenge.challenge_id, x: nextPosition }),
        signal: controller.signal,
      });
      if (version !== requestVersion.current) return;
      if (typeof result.proof !== "string" || !result.proof || !Number.isFinite(result.expires_in) || result.expires_in <= 0) throw new Error("Invalid captcha proof");
      setExpiresAt(startedAt + result.expires_in * 1000);
      setPhase("verified");
      onProofChange(result.proof);
    } catch (error) {
      if (version !== requestVersion.current || (controller.signal.aborted && !timedOut)) return;
      setMessage(timedOut ? "连接超时，请刷新" : failureMessage(error));
      setExpiresAt(null);
      setPhase("error");
    } finally {
      window.clearTimeout(timer);
      if (version === requestVersion.current) verifyingRef.current = false;
    }
  };

  const imageFailed = () => {
    onProofChange(null);
    setMessage("验证码图片加载失败，请刷新");
    setExpiresAt(null);
    setPhase("error");
  };
  const loading = phase === "loading" || (phase === "ready" && !imagesReady);
  const status = disabled && phase === "verified" ? "正在登录…" : loading ? "正在加载拼图…" : phase === "verifying" ? "正在验证…" : phase === "verified" ? "验证通过" : phase === "expired" ? "验证已过期，请刷新" : phase === "error" ? message : "拖动拼块至缺口";

  return <TooltipProvider delayDuration={150}><div className="cloud-captcha" data-testid="cloud-captcha" data-phase={phase} aria-busy={loading || phase === "verifying"}>
    <div className="cloud-captcha-heading">
      <span role={phase === "error" ? "alert" : "status"} aria-live="polite" data-testid="captcha-status">
        {phase === "verified" && (disabled ? <Loader2 className="cloud-spin" size={16} aria-hidden="true" /> : <Check size={16} aria-hidden="true" />)}{status}
      </span>
      <Tooltip><TooltipTrigger asChild><button type="button" className="cloud-icon-button cloud-captcha-refresh" data-testid="captcha-refresh" aria-label="刷新拼图" disabled={disabled || loading || phase === "verifying"} onClick={() => void refresh()}><RefreshCw size={16} aria-hidden="true" /></button></TooltipTrigger><TooltipContent>刷新拼图</TooltipContent></Tooltip>
    </div>
    <div className="cloud-captcha-image" data-testid="captcha-image" style={{ aspectRatio: challenge ? `${challenge.image_width} / ${challenge.image_height}` : "2 / 1" }}>
      {challenge && <>
        <Image key={`${challenge.challenge_id}-background`} className="cloud-captcha-background" src={challenge.background_image} width={challenge.image_width} height={challenge.image_height} alt="" unoptimized draggable={false} onLoad={() => setLoadedImages(previous => ({ ...previous, background: true }))} onError={imageFailed} />
        <Image key={`${challenge.challenge_id}-piece`} data-testid="captcha-piece" className="cloud-captcha-piece" src={challenge.piece_image} width={challenge.piece_width} height={challenge.piece_height} alt="" unoptimized draggable={false} style={{ left: `${position / challenge.image_width * 100}%`, top: `${challenge.piece_y / challenge.image_height * 100}%`, width: `${challenge.piece_width / challenge.image_width * 100}%`, height: `${challenge.piece_height / challenge.image_height * 100}%` }} onLoad={() => setLoadedImages(previous => ({ ...previous, piece: true }))} onError={imageFailed} />
      </>}
      {(loading || phase === "verifying") && <div className="cloud-captcha-loading" aria-hidden="true"><Loader2 className="cloud-spin" size={20} /></div>}
    </div>
    <input id={sliderId} className="cloud-captcha-range" data-testid="captcha-slider" type="range" min={0} max={maxPosition} step={1} value={position} aria-label="拖动拼块至缺口" aria-valuetext={`${Math.round(position / maxPosition * 100)}%`} aria-describedby={hintId} disabled={disabled || phase !== "ready" || !imagesReady} onChange={event => setPosition(Number(event.currentTarget.value))} onPointerDown={event => event.currentTarget.setPointerCapture(event.pointerId)} onPointerUp={event => void verify(Number(event.currentTarget.value))} onKeyDown={event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        event.stopPropagation();
        void verify(Number(event.currentTarget.value));
      }
    }} />
    <span className="sr-only" id={hintId}>方向键调整位置，Home 或 End 移至两端，按 Enter 或空格验证。</span>
  </div></TooltipProvider>;
}
